#!/usr/bin/env python3
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, unquote, urlparse, urlunparse

import requests
from requests.auth import HTTPDigestAuth
from zeep.helpers import serialize_object

from camera import (
    DEFAULT_IP,
    DEFAULT_PASS,
    DEFAULT_PORT,
    DEFAULT_PROFILE,
    DEFAULT_USER,
    DIRECTIONS,
    clamp,
    connect,
    get_services,
    move_raw,
    stop_raw,
)


ROOT = Path(__file__).resolve().parent


def load_dotenv(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(ROOT / ".env")


def save_dotenv(updates):
    path = ROOT / ".env"
    lines = []
    seen = set()

    try:
        existing = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        existing = []

    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            lines.append(line)
            continue

        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            lines.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            lines.append(line)

    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apply_runtime_config(updates):
    global STREAM_URL, DIRECT_STREAM, RTSP_TRANSPORT, VIDEO_PROFILE, AUDIO_PROFILE, ENABLE_ZOOM
    global SNAPSHOT_DIR, RECORDING_DIR

    for key, value in updates.items():
        os.environ[key] = str(value)

    STREAM_URL = os.getenv("CAMERA_STREAM_URL", "")
    DIRECT_STREAM = os.getenv("CAMERA_DIRECT_STREAM", "0") == "1"
    RTSP_TRANSPORT = os.getenv("CAMERA_RTSP_TRANSPORT", "udp").lower()
    VIDEO_PROFILE = os.getenv("CAMERA_VIDEO_PROFILE", "")
    AUDIO_PROFILE = os.getenv("CAMERA_AUDIO_PROFILE", DEFAULT_PROFILE)
    ENABLE_ZOOM = os.getenv("CAMERA_ENABLE_ZOOM", "auto").lower()
    SNAPSHOT_DIR = Path(os.getenv("CAMERA_SNAPSHOT_DIR", str(ROOT / "snapshots")))
    RECORDING_DIR = Path(os.getenv("CAMERA_RECORDING_DIR", str(ROOT / "recordings")))


APP_HOST = os.getenv("CAMERA_UI_HOST", "0.0.0.0")
APP_PORT = int(os.getenv("CAMERA_UI_PORT", "8000"))
STREAM_URL = os.getenv("CAMERA_STREAM_URL", "")
DIRECT_STREAM = os.getenv("CAMERA_DIRECT_STREAM", "0") == "1"
RTSP_TRANSPORT = os.getenv("CAMERA_RTSP_TRANSPORT", "udp").lower()
VIDEO_PROFILE = os.getenv("CAMERA_VIDEO_PROFILE", "")
AUDIO_PROFILE = os.getenv("CAMERA_AUDIO_PROFILE", DEFAULT_PROFILE)
ENABLE_ZOOM = os.getenv("CAMERA_ENABLE_ZOOM", "auto").lower()
SNAPSHOT_DIR = Path(os.getenv("CAMERA_SNAPSHOT_DIR", str(ROOT / "snapshots")))
RECORDING_DIR = Path(os.getenv("CAMERA_RECORDING_DIR", str(ROOT / "recordings")))
RECORDING_LOCK = threading.Lock()
RECORDING = {
    "process": None,
    "path": None,
    "filename": None,
    "started_at": None,
    "stderr": None,
}
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
THUMB_DIR = ROOT / ".thumbs"


def camera_args(timeout=5.0):
    return SimpleNamespace(
        host=os.getenv("CAMERA_IP", DEFAULT_IP),
        port=int(os.getenv("CAMERA_PORT", str(DEFAULT_PORT))),
        user=os.getenv("CAMERA_USER", DEFAULT_USER),
        password=os.getenv("CAMERA_PASS", DEFAULT_PASS),
        profile=os.getenv("CAMERA_PROFILE", DEFAULT_PROFILE),
        timeout=timeout,
        no_digest=False,
        adjust_time=False,
    )


def get_rtsp_uri(profile_token=None):
    args = camera_args(timeout=4.0)
    profile = profile_token or VIDEO_PROFILE or args.profile
    try:
        camera = connect(args)
        media, _ = get_services(camera)
        request = media.create_type("GetStreamUri")
        request.ProfileToken = profile
        request.StreamSetup = {
            "Stream": "RTP-Unicast",
            "Transport": {"Protocol": "RTSP"},
        }
        return media.GetStreamUri(request).Uri
    except Exception:
        path = "onvif2" if profile == "IPCProfilesToken1" else "onvif1"
        return f"rtsp://{args.host}:554/{path}"


def profile_supports_zoom(profile):
    ptz_config = getattr(profile, "PTZConfiguration", None)
    if not ptz_config:
        return False

    zoom_fields = [
        "DefaultAbsoluteZoomPositionSpace",
        "DefaultRelativeZoomTranslationSpace",
        "DefaultContinuousZoomVelocitySpace",
        "ZoomLimits",
    ]
    return any(getattr(ptz_config, field, None) for field in zoom_fields)


def camera_supports_zoom():
    if ENABLE_ZOOM in {"1", "true", "yes", "on"}:
        return True
    if ENABLE_ZOOM in {"0", "false", "no", "off"}:
        return False

    args = camera_args(timeout=4.0)
    try:
        camera = connect(args)
        media, _ = get_services(camera)
        profiles = media.GetProfiles()
    except Exception:
        return False

    for profile in profiles:
        if profile.token == args.profile:
            return profile_supports_zoom(profile)

    return any(profile_supports_zoom(profile) for profile in profiles)


def basic_device_info():
    args = camera_args(timeout=4.0)
    try:
        camera = connect(args)
        info = normalize_for_json(camera.devicemgmt.GetDeviceInformation())
    except Exception:
        return {}

    if not isinstance(info, dict):
        return {}

    return {
        "manufacturer": info.get("Manufacturer"),
        "model": info.get("Model"),
        "firmwareVersion": info.get("FirmwareVersion"),
        "serialNumber": info.get("SerialNumber"),
        "hardwareId": info.get("HardwareId"),
    }


def basic_profiles_info():
    args = camera_args(timeout=4.0)
    try:
        camera = connect(args)
        media, _ = get_services(camera)
        profiles = normalize_for_json(media.GetProfiles())
    except Exception:
        return []

    return summarize_profiles(profiles)


def stream_kind(url):
    if not url:
        return "rtsp"

    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return "http"
    if parsed.scheme == "rtsp":
        return "rtsp"
    return "unknown"


def rtsp_url_with_auth(url, args):
    parsed = urlparse(url)
    if parsed.scheme != "rtsp" or parsed.username:
        return url

    user = quote(args.user, safe="")
    password = quote(args.password, safe="")
    netloc = f"{user}:{password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"

    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None


def mask_url(url):
    parsed = urlparse(url)
    if not parsed.password:
        return url

    username = parsed.username or ""
    netloc = f"{username}:***@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(
        (
            parsed.scheme,
            netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def ffmpeg_rtsp_input_args():
    if RTSP_TRANSPORT in {"tcp", "udp", "udp_multicast", "http", "https"}:
        return ["-rtsp_transport", RTSP_TRANSPORT]
    return []


def build_ffmpeg_stream_command(ffmpeg, url):
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        *ffmpeg_rtsp_input_args(),
        "-i",
        url,
        "-an",
        "-map",
        "0:v:0",
        "-vf",
        "scale=960:-1",
        "-r",
        "8",
        "-q:v",
        "5",
        "-f",
        "mjpeg",
        "pipe:1",
    ]


def build_ffmpeg_test_command(ffmpeg, url):
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        *ffmpeg_rtsp_input_args(),
        "-i",
        url,
        "-an",
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]


def build_ffmpeg_audio_command(ffmpeg, url):
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        *ffmpeg_rtsp_input_args(),
        "-allowed_media_types",
        "audio",
        "-i",
        url,
        "-vn",
        "-map",
        "0:a:0",
        "-acodec",
        "libmp3lame",
        "-ar",
        "44100",
        "-ac",
        "1",
        "-b:a",
        "64k",
        "-f",
        "mp3",
        "pipe:1",
    ]


def build_ffmpeg_snapshot_command(ffmpeg, url, output_path):
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        *ffmpeg_rtsp_input_args(),
        "-i",
        url,
        "-an",
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-y",
        str(output_path),
    ]


def build_ffmpeg_record_command(ffmpeg, video_url, audio_url, output_path):
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        *ffmpeg_rtsp_input_args(),
        "-i",
        video_url,
        *ffmpeg_rtsp_input_args(),
        "-allowed_media_types",
        "audio",
        "-i",
        audio_url,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        "scale=960:-2",
        "-r",
        "15",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-ar",
        "44100",
        "-ac",
        "1",
        "-movflags",
        "+faststart",
        "-y",
        str(output_path),
    ]


def drain_stderr(process):
    tail = deque(maxlen=120)

    def read_stderr():
        if not process.stderr:
            return

        for raw_line in iter(process.stderr.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                tail.append(line)

    thread = threading.Thread(target=read_stderr, daemon=True)
    thread.start()
    return tail


def json_response(handler, status, payload):
    body = json.dumps(payload, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def recording_thumb(path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    try:
        THUMB_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    thumb_name = f"{path.stem}.jpg"
    thumb_path = THUMB_DIR / thumb_name
    try:
        if thumb_path.exists() and thumb_path.stat().st_mtime >= path.stat().st_mtime:
            return f"/media/thumbs/{quote(thumb_name)}"
    except OSError:
        return None

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "1",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        "scale=320:-2",
        str(thumb_path),
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=False)
    except subprocess.TimeoutExpired:
        return None

    if result.returncode != 0 or not thumb_path.exists():
        return None

    return f"/media/thumbs/{quote(thumb_name)}"


def media_items(kind):
    if kind == "snapshots":
        directory = SNAPSHOT_DIR
        pattern = "*.jpg"
    elif kind == "recordings":
        directory = RECORDING_DIR
        pattern = "*.mp4"
    else:
        return None

    try:
        files = sorted(
            directory.glob(pattern),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        files = []

    items = []
    for path in files:
        if not path.is_file():
            continue

        item = {
            "name": path.name,
            "url": f"/media/{kind}/{quote(path.name)}",
            "bytes": path.stat().st_size,
            "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        }
        if kind == "recordings":
            item["thumbUrl"] = recording_thumb(path)
        items.append(item)

    return items


def normalize_for_json(value):
    value = serialize_object(value)
    if isinstance(value, dict):
        return {str(key): normalize_for_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_for_json(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "tag") and hasattr(value, "attrib"):
        return str(value)
    return value


def safe_onvif_call(label, callback):
    try:
        return {"ok": True, "label": label, "value": normalize_for_json(callback())}
    except Exception as exc:
        return {"ok": False, "label": label, "error": str(exc)}


def section_value(sections, label):
    for section in sections:
        if section.get("label") == label and section.get("ok"):
            return section.get("value")
    return None


def summarize_capabilities(capabilities):
    if not isinstance(capabilities, dict):
        return []

    names = []
    for key, value in capabilities.items():
        if value:
            names.append(str(key))
    return names


def summarize_services(services):
    if not isinstance(services, list):
        return []

    summary = []
    for service in services:
        if not isinstance(service, dict):
            continue
        namespace = service.get("Namespace", "")
        xaddr = service.get("XAddr", "")
        version = service.get("Version", {})
        version_text = ""
        if isinstance(version, dict):
            major = version.get("Major")
            minor = version.get("Minor")
            if major is not None and minor is not None:
                version_text = f"{major}.{minor}"
        summary.append({"namespace": namespace, "version": version_text, "xaddr": xaddr})
    return summary


def summarize_profiles(profiles):
    if not isinstance(profiles, list):
        return []

    summary = []
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        video = profile.get("VideoEncoderConfiguration") or {}
        resolution = video.get("Resolution") if isinstance(video, dict) else {}
        width = resolution.get("Width") if isinstance(resolution, dict) else None
        height = resolution.get("Height") if isinstance(resolution, dict) else None
        summary.append(
            {
                "token": profile.get("token"),
                "name": profile.get("Name"),
                "videoEncoding": video.get("Encoding") if isinstance(video, dict) else None,
                "resolution": f"{width}x{height}" if width and height else None,
                "hasPTZ": bool(profile.get("PTZConfiguration")),
                "hasAudio": bool(profile.get("AudioEncoderConfiguration")),
            }
        )
    return summary


def identification_summary(args, sections):
    device_info = section_value(sections, "Device information") or {}
    capabilities = section_value(sections, "Capabilities")
    services = section_value(sections, "Services")
    profiles = section_value(sections, "Profiles")

    return {
        "connection": {
            "host": args.host,
            "port": args.port,
            "user": args.user,
            "profile": args.profile,
            "videoProfile": VIDEO_PROFILE or args.profile,
            "audioProfile": AUDIO_PROFILE,
        },
        "ptz": {
            "zoomMode": ENABLE_ZOOM,
            "supportsZoom": camera_supports_zoom(),
        },
        "network": network_status(),
        "device": {
            "manufacturer": device_info.get("Manufacturer"),
            "model": device_info.get("Model"),
            "firmwareVersion": device_info.get("FirmwareVersion"),
            "serialNumber": device_info.get("SerialNumber"),
            "hardwareId": device_info.get("HardwareId"),
        },
        "capabilities": summarize_capabilities(capabilities),
        "services": summarize_services(services),
        "profiles": summarize_profiles(profiles),
    }


def camera_identification():
    args = camera_args(timeout=6.0)
    camera = connect(args)
    device = camera.devicemgmt
    sections = []

    sections.append(safe_onvif_call("Device information", device.GetDeviceInformation))
    sections.append(safe_onvif_call("Hostname", device.GetHostname))
    sections.append(safe_onvif_call("System date and time", device.GetSystemDateAndTime))
    sections.append(safe_onvif_call("Network interfaces", device.GetNetworkInterfaces))
    sections.append(safe_onvif_call("Network protocols", device.GetNetworkProtocols))
    sections.append(safe_onvif_call("DNS", device.GetDNS))
    sections.append(safe_onvif_call("NTP", device.GetNTP))
    sections.append(safe_onvif_call("Scopes", device.GetScopes))
    sections.append(safe_onvif_call("Capabilities", lambda: device.GetCapabilities({"Category": "All"})))
    sections.append(safe_onvif_call("Services", lambda: device.GetServices({"IncludeCapability": True})))

    try:
        media, _ = get_services(camera)
        sections.append(safe_onvif_call("Profiles", media.GetProfiles))
    except Exception as exc:
        sections.append({"ok": False, "label": "Profiles", "error": str(exc)})

    return {
        "target": f"{args.host}:{args.port}",
        "user": args.user,
        "profile": args.profile,
        "summary": identification_summary(args, sections),
        "sections": sections,
    }


def setup_payload():
    args = camera_args(timeout=5.0)
    profiles = basic_profiles_info()
    return {
        "camera": {
            "ip": args.host,
            "port": args.port,
            "user": args.user,
            "profile": args.profile,
            "videoProfile": VIDEO_PROFILE or args.profile,
            "audioProfile": AUDIO_PROFILE,
            "rtspTransport": RTSP_TRANSPORT,
            "enableZoom": ENABLE_ZOOM,
            "uiHost": APP_HOST,
            "uiPort": APP_PORT,
            "snapshotDir": str(SNAPSHOT_DIR),
            "recordingDir": str(RECORDING_DIR),
            "streamUrl": STREAM_URL,
            "directStream": DIRECT_STREAM,
        },
        "profiles": profiles,
        "device": basic_device_info(),
    }


def ipconfig_wifi_ipv4():
    if platform.system().lower() != "windows":
        return None

    try:
        result = subprocess.run(
            ["ipconfig"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except Exception:
        return None

    current_adapter = ""
    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip()
        if line and not line.startswith(" ") and line.endswith(":"):
            current_adapter = line.strip(":")
            continue

        if "Wireless LAN adapter Wi-Fi" not in current_adapter:
            continue

        if "IPv4" in line and ":" in line:
            return line.split(":", 1)[1].strip()

    return None


def fallback_local_ipv4():
    target = os.getenv("CAMERA_IP", DEFAULT_IP) or "8.8.8.8"
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((target, 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return None
    finally:
        sock.close()


def windows_firewall_port_status(port):
    if platform.system().lower() != "windows":
        return {"status": "unknown", "message": "Verificacao automatica de firewall disponivel apenas no Windows."}

    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=Camera UI 8080"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "message": "Nao foi possivel verificar o firewall em tempo habil."}
    except Exception:
        return {"status": "unknown", "message": "Nao foi possivel consultar o firewall."}

    if result.returncode != 0:
        return {"status": "not_found", "message": f"Nao encontrei regra inbound TCP permitindo a porta {port}."}

    output = result.stdout.lower()
    expected_port = str(port)
    if "allow" in output and "in" in output and ("tcp" in output or "any" in output) and expected_port in output:
        return {"status": "allowed", "message": f"Existe regra inbound TCP permitindo a porta {port}."}
    return {"status": "not_found", "message": f"Nao encontrei regra inbound TCP permitindo a porta {port}."}


def network_status():
    ipv4 = ipconfig_wifi_ipv4() or fallback_local_ipv4()
    port = APP_PORT
    return {
        "host": APP_HOST,
        "port": port,
        "ipv4": ipv4,
        "url": f"http://{ipv4}:{port}" if ipv4 else None,
        "firewall": windows_firewall_port_status(port),
    }


def recording_status():
    with RECORDING_LOCK:
        process = RECORDING["process"]
        if process and process.poll() is not None:
            RECORDING["process"] = None

        running = RECORDING["process"] is not None
        path = RECORDING["path"]
        size = path.stat().st_size if path and path.exists() else 0
        return {
            "running": running,
            "filename": RECORDING["filename"],
            "path": str(path) if path else None,
            "startedAt": RECORDING["started_at"],
            "bytes": size,
        }


class CameraHandler(BaseHTTPRequestHandler):
    server_version = "CameraUI/1.0"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path in {"/", "/index.html"}:
            self.serve_file(ROOT / "index.html", "text/html; charset=utf-8")
            return

        if parsed.path == "/api/config":
            args = camera_args()
            url = STREAM_URL or get_rtsp_uri()
            kind = stream_kind(url)
            ffmpeg_path = shutil.which("ffmpeg")
            supports_zoom = camera_supports_zoom()
            device_info = basic_device_info()
            profiles_info = basic_profiles_info()
            json_response(
                self,
                200,
                {
                    "camera": f"{args.host}:{args.port}",
                    "profile": args.profile,
                    "profiles": profiles_info,
                    "device": device_info,
                    "videoProfile": VIDEO_PROFILE or args.profile,
                    "audioProfile": AUDIO_PROFILE,
                    "streamUrl": url,
                    "streamKind": kind,
                    "directStream": DIRECT_STREAM and kind == "http",
                    "ffmpegAvailable": ffmpeg_path is not None,
                    "ffmpegPath": ffmpeg_path,
                    "rtspTransport": RTSP_TRANSPORT,
                    "hasBrowserStream": kind == "http" or (kind == "rtsp" and ffmpeg_path is not None),
                    "hasAudioStream": kind == "rtsp" and ffmpeg_path is not None,
                    "supportsZoom": supports_zoom,
                    "snapshotDir": str(SNAPSHOT_DIR),
                    "recordingDir": str(RECORDING_DIR),
                    "recording": recording_status(),
                },
            )
            return

        if parsed.path == "/api/stream-test":
            self.test_stream()
            return

        if parsed.path == "/api/identification":
            self.identification()
            return

        if parsed.path == "/api/setup":
            json_response(self, 200, {"ok": True, **setup_payload()})
            return

        if parsed.path == "/api/network-status":
            json_response(self, 200, {"ok": True, **network_status()})
            return

        if parsed.path == "/api/media/snapshots":
            json_response(self, 200, {"ok": True, "items": media_items("snapshots") or []})
            return

        if parsed.path == "/api/media/recordings":
            json_response(self, 200, {"ok": True, "items": media_items("recordings") or []})
            return

        if parsed.path.startswith("/media/"):
            self.serve_media(parsed.path)
            return

        if parsed.path == "/video":
            self.proxy_video()
            return

        if parsed.path == "/audio":
            self.stream_audio()
            return

        json_response(self, 404, {"ok": False, "error": "Nao encontrado"})

    def identification(self):
        try:
            payload = camera_identification()
        except Exception as exc:
            json_response(self, 502, {"ok": False, "error": str(exc)})
            return

        payload["ok"] = True
        json_response(self, 200, payload)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/snapshot":
            self.capture_snapshot()
            return

        if parsed.path == "/api/record":
            self.handle_recording()
            return

        if parsed.path == "/api/setup":
            self.save_setup()
            return

        if parsed.path != "/api/move":
            json_response(self, 404, {"ok": False, "error": "Nao encontrado"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length).decode("utf-8") if length else "{}"

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            json_response(self, 400, {"ok": False, "error": "JSON invalido"})
            return

        command = payload.get("command")
        speed = clamp(float(payload.get("speed", 0.35)), 0.0, 1.0)
        duration = max(0.0, min(float(payload.get("duration", 0.25)), 2.0))
        args = camera_args(timeout=5.0)

        try:
            if command == "stop":
                stop_raw(args, args.profile)
            elif command in {"zoom-in", "zoom-out", "zoom_in", "zoom_out", "aproximar", "afastar"} and not camera_supports_zoom():
                json_response(self, 400, {"ok": False, "error": "Esta camera nao anuncia suporte a zoom PTZ."})
                return
            elif command in DIRECTIONS:
                pan, tilt, zoom = DIRECTIONS[command]
                move_raw(
                    args,
                    args.profile,
                    pan * speed,
                    tilt * speed,
                    zoom * speed,
                    duration,
                )
            else:
                json_response(self, 400, {"ok": False, "error": "Comando invalido"})
                return
        except Exception as exc:
            json_response(self, 502, {"ok": False, "error": str(exc)})
            return

        json_response(
            self,
            200,
            {
                "ok": True,
                "command": command,
                "speed": speed,
                "duration": duration,
                "profile": args.profile,
            },
        )

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/media/snapshots/"):
            self.delete_snapshot(parsed.path)
            return

        if parsed.path.startswith("/api/media/recordings/"):
            self.delete_recording(parsed.path)
            return

        json_response(self, 404, {"ok": False, "error": "Nao encontrado"})

    def delete_snapshot(self, request_path):
        prefix = "/api/media/snapshots/"
        filename = unquote(request_path[len(prefix) :])
        if Path(filename).name != filename:
            json_response(self, 400, {"ok": False, "error": "Nome de arquivo invalido"})
            return

        path = SNAPSHOT_DIR / filename
        if path.suffix.lower() != ".jpg" or not path.is_file():
            json_response(self, 404, {"ok": False, "error": "Arquivo nao encontrado"})
            return

        try:
            path.unlink()
        except OSError as exc:
            json_response(self, 500, {"ok": False, "error": f"Nao foi possivel excluir: {exc}"})
            return

        json_response(self, 200, {"ok": True, "filename": filename})

    def delete_recording(self, request_path):
        prefix = "/api/media/recordings/"
        filename = unquote(request_path[len(prefix) :])
        if Path(filename).name != filename:
            json_response(self, 400, {"ok": False, "error": "Nome de arquivo invalido"})
            return

        path = RECORDING_DIR / filename
        if path.suffix.lower() != ".mp4" or not path.is_file():
            json_response(self, 404, {"ok": False, "error": "Arquivo nao encontrado"})
            return

        try:
            path.unlink()
            thumb = THUMB_DIR / f"{path.stem}.jpg"
            if thumb.exists():
                thumb.unlink()
        except OSError as exc:
            json_response(self, 500, {"ok": False, "error": f"Nao foi possivel excluir: {exc}"})
            return

        json_response(self, 200, {"ok": True, "filename": filename})

    def capture_snapshot(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            json_response(
                self,
                501,
                {
                    "ok": False,
                    "error": "Snapshot RTSP precisa de ffmpeg.",
                },
            )
            return

        args = camera_args(timeout=8.0)
        url = rtsp_url_with_auth(STREAM_URL or get_rtsp_uri(), args)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"snapshot-{timestamp}.jpg"
        output_path = SNAPSHOT_DIR / filename

        try:
            SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            json_response(
                self,
                500,
                {
                    "ok": False,
                    "error": f"Nao foi possivel criar a pasta de snapshots: {exc}",
                },
            )
            return

        command = build_ffmpeg_snapshot_command(ffmpeg, url, output_path)
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
            json_response(
                self,
                504,
                {
                    "ok": False,
                    "error": "Timeout capturando snapshot.",
                    "detail": stderr[-4000:],
                },
            )
            return

        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        if result.returncode != 0 or not output_path.exists():
            json_response(
                self,
                502,
                {
                    "ok": False,
                    "error": "ffmpeg nao conseguiu salvar o snapshot.",
                    "detail": stderr[-4000:],
                },
            )
            return

        json_response(
            self,
            200,
            {
                "ok": True,
                "filename": filename,
                "path": str(output_path),
                "bytes": output_path.stat().st_size,
            },
        )

    def handle_recording(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length).decode("utf-8") if length else "{}"

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            json_response(self, 400, {"ok": False, "error": "JSON invalido"})
            return

        action = payload.get("action")
        if action == "start":
            self.start_recording()
            return

        if action == "stop":
            self.stop_recording()
            return

        json_response(self, 400, {"ok": False, "error": "Acao de gravacao invalida"})

    def save_setup(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length).decode("utf-8") if length else "{}"

        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            json_response(self, 400, {"ok": False, "error": "JSON invalido"})
            return

        allowed = {
            "CAMERA_IP",
            "CAMERA_PORT",
            "CAMERA_USER",
            "CAMERA_PASS",
            "CAMERA_PROFILE",
            "CAMERA_RTSP_TRANSPORT",
            "CAMERA_VIDEO_PROFILE",
            "CAMERA_AUDIO_PROFILE",
            "CAMERA_ENABLE_ZOOM",
            "CAMERA_SNAPSHOT_DIR",
            "CAMERA_RECORDING_DIR",
            "CAMERA_STREAM_URL",
            "CAMERA_DIRECT_STREAM",
        }
        updates = {}
        for key in allowed:
            if key not in payload:
                continue
            value = str(payload.get(key, "")).strip()
            if key == "CAMERA_PASS" and not value:
                continue
            updates[key] = value

        if "CAMERA_PORT" in updates:
            try:
                int(updates["CAMERA_PORT"])
            except ValueError:
                json_response(self, 400, {"ok": False, "error": "CAMERA_PORT invalida"})
                return

        if updates.get("CAMERA_RTSP_TRANSPORT") not in {None, "udp", "tcp", "udp_multicast", "http", "https"}:
            json_response(self, 400, {"ok": False, "error": "CAMERA_RTSP_TRANSPORT invalido"})
            return

        if updates.get("CAMERA_ENABLE_ZOOM") not in {None, "auto", "0", "1"}:
            json_response(self, 400, {"ok": False, "error": "CAMERA_ENABLE_ZOOM invalido"})
            return

        try:
            save_dotenv(updates)
            apply_runtime_config(updates)
        except OSError as exc:
            json_response(self, 500, {"ok": False, "error": f"Nao foi possivel salvar .env: {exc}"})
            return

        json_response(self, 200, {"ok": True, **setup_payload()})

    def start_recording(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            json_response(
                self,
                501,
                {
                    "ok": False,
                    "error": "Gravacao RTSP precisa de ffmpeg.",
                },
            )
            return

        with RECORDING_LOCK:
            process = RECORDING["process"]
            if process and process.poll() is None:
                json_response(
                    self,
                    409,
                    {
                        "ok": False,
                        "error": "Ja existe uma gravacao em andamento.",
                    },
                )
                return

        args = camera_args(timeout=8.0)
        video_url = rtsp_url_with_auth(STREAM_URL or get_rtsp_uri(), args)
        audio_url = rtsp_url_with_auth(get_rtsp_uri(AUDIO_PROFILE), args)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"recording-{timestamp}.mp4"
        output_path = RECORDING_DIR / filename

        try:
            RECORDING_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            json_response(
                self,
                500,
                {
                    "ok": False,
                    "error": f"Nao foi possivel criar a pasta de gravacoes: {exc}",
                },
            )
            return

        command = build_ffmpeg_record_command(ffmpeg, video_url, audio_url, output_path)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        stderr_tail = drain_stderr(process)
        time.sleep(0.6)
        if process.poll() is not None:
            detail = "\n".join(stderr_tail)
            json_response(
                self,
                502,
                {
                    "ok": False,
                    "error": "ffmpeg encerrou antes de iniciar a gravacao.",
                    "detail": detail[-4000:],
                },
            )
            return

        with RECORDING_LOCK:
            RECORDING.update(
                {
                    "process": process,
                    "path": output_path,
                    "filename": filename,
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                    "stderr": stderr_tail,
                }
            )

        json_response(
            self,
            200,
            {
                "ok": True,
                "recording": recording_status(),
            },
        )

    def stop_recording(self):
        with RECORDING_LOCK:
            process = RECORDING["process"]
            output_path = RECORDING["path"]
            filename = RECORDING["filename"]
            stderr_tail = RECORDING["stderr"]

            if not process or process.poll() is not None:
                RECORDING["process"] = None
                json_response(
                    self,
                    409,
                    {
                        "ok": False,
                        "error": "Nao ha gravacao em andamento.",
                    },
                )
                return

        try:
            if process.stdin:
                process.stdin.write(b"q")
                process.stdin.flush()
            process.wait(timeout=8)
        except Exception:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=4)

        detail = "\n".join(stderr_tail or [])
        bytes_written = output_path.stat().st_size if output_path and output_path.exists() else 0

        with RECORDING_LOCK:
            RECORDING.update(
                {
                    "process": None,
                    "path": output_path,
                    "filename": filename,
                    "started_at": None,
                    "stderr": None,
                }
            )

        json_response(
            self,
            200,
            {
                "ok": True,
                "filename": filename,
                "path": str(output_path) if output_path else None,
                "bytes": bytes_written,
                "detail": detail[-4000:],
            },
        )

    def serve_file(self, path, content_type):
        try:
            body = path.read_bytes()
        except OSError:
            json_response(self, 404, {"ok": False, "error": "Arquivo nao encontrado"})
            return

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def serve_media(self, request_path):
        parts = request_path.split("/", 3)
        if len(parts) != 4:
            json_response(self, 404, {"ok": False, "error": "Arquivo nao encontrado"})
            return

        kind = parts[2]
        filename = unquote(parts[3])
        if Path(filename).name != filename:
            json_response(self, 400, {"ok": False, "error": "Nome de arquivo invalido"})
            return

        if kind == "thumbs":
            base_dir = THUMB_DIR
            content_type = "image/jpeg"
            allowed_suffix = ".jpg"
        elif kind == "snapshots":
            base_dir = SNAPSHOT_DIR
            content_type = "image/jpeg"
            allowed_suffix = ".jpg"
        elif kind == "recordings":
            base_dir = RECORDING_DIR
            content_type = "video/mp4"
            allowed_suffix = ".mp4"
        else:
            json_response(self, 404, {"ok": False, "error": "Arquivo nao encontrado"})
            return

        path = base_dir / filename
        if path.suffix.lower() != allowed_suffix or not path.is_file():
            json_response(self, 404, {"ok": False, "error": "Arquivo nao encontrado"})
            return

        self.serve_file(path, content_type)

    def proxy_video(self):
        url = STREAM_URL or get_rtsp_uri()
        kind = stream_kind(url)

        if kind == "rtsp":
            self.stream_rtsp(url)
            return

        if not url or kind != "http":
            json_response(
                self,
                501,
                {
                    "ok": False,
                    "error": "Configure CAMERA_STREAM_URL com uma URL HTTP/MJPEG para exibir no navegador.",
                },
            )
            return

        args = camera_args(timeout=8.0)
        parsed = urlparse(self.path)
        refresh = parse_qs(parsed.query).get("refresh", ["0"])[0]
        headers = {"Cache-Control": "no-cache"} if refresh else {}

        try:
            response = requests.get(
                url,
                headers=headers,
                auth=HTTPDigestAuth(args.user, args.password),
                stream=True,
                timeout=10,
            )
            response.raise_for_status()
        except Exception as exc:
            json_response(self, 502, {"ok": False, "error": str(exc)})
            return

        content_type = response.headers.get("Content-Type", "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        try:
            for chunk in response.iter_content(chunk_size=16384):
                if not chunk:
                    break
                self.wfile.write(chunk)
        except CLIENT_DISCONNECT_ERRORS:
            pass
        finally:
            response.close()

    def stream_rtsp(self, url):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            json_response(
                self,
                501,
                {
                    "ok": False,
                    "error": "RTSP precisa de ffmpeg para aparecer no navegador.",
                },
            )
            return

        args = camera_args(timeout=8.0)
        url = rtsp_url_with_auth(url, args)
        command = build_ffmpeg_stream_command(ffmpeg, url)
        print(f"Iniciando ffmpeg RTSP: {mask_url(url)} transport={RTSP_TRANSPORT}")

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stderr_tail = drain_stderr(process)
        time.sleep(0.3)
        if process.poll() is not None:
            json_response(
                self,
                502,
                {
                    "ok": False,
                    "error": "ffmpeg encerrou antes de entregar video.",
                    "detail": "\n".join(stderr_tail)[-4000:],
                },
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        buffer = b""
        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk

                while True:
                    start = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9", start + 2)
                    if start == -1 or end == -1:
                        if start > 0:
                            buffer = buffer[start:]
                        break

                    frame = buffer[start : end + 2]
                    buffer = buffer[end + 2 :]
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
        except CLIENT_DISCONNECT_ERRORS:
            pass
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

            stderr = "\n".join(stderr_tail)
            if stderr:
                print(f"ffmpeg RTSP encerrou com diagnostico:\n{stderr[-4000:]}")

    def stream_audio(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            json_response(
                self,
                501,
                {
                    "ok": False,
                    "error": "Audio RTSP precisa de ffmpeg para tocar no navegador.",
                },
            )
            return

        args = camera_args(timeout=8.0)
        url = rtsp_url_with_auth(get_rtsp_uri(AUDIO_PROFILE), args)
        command = build_ffmpeg_audio_command(ffmpeg, url)
        print(f"Iniciando ffmpeg audio: {mask_url(url)} transport={RTSP_TRANSPORT}")

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stderr_tail = drain_stderr(process)
        time.sleep(0.4)
        if process.poll() is not None:
            json_response(
                self,
                502,
                {
                    "ok": False,
                    "error": "ffmpeg encerrou antes de entregar audio.",
                    "detail": "\n".join(stderr_tail)[-4000:],
                },
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        try:
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except CLIENT_DISCONNECT_ERRORS:
            pass
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

            stderr = "\n".join(stderr_tail)
            if stderr:
                print(f"ffmpeg audio encerrou com diagnostico:\n{stderr[-4000:]}")

    def test_stream(self):
        url = STREAM_URL or get_rtsp_uri()
        kind = stream_kind(url)
        ffmpeg = shutil.which("ffmpeg")

        if kind != "rtsp":
            json_response(
                self,
                200,
                {
                    "ok": kind == "http",
                    "streamKind": kind,
                    "message": "O teste de ffmpeg so se aplica a RTSP.",
                },
            )
            return

        if not ffmpeg:
            json_response(
                self,
                501,
                {
                    "ok": False,
                    "error": "ffmpeg nao encontrado no PATH do Windows.",
                },
            )
            return

        args = camera_args(timeout=8.0)
        url = rtsp_url_with_auth(url, args)
        command = build_ffmpeg_test_command(ffmpeg, url)

        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=12,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
            json_response(
                self,
                504,
                {
                    "ok": False,
                    "error": "Timeout aguardando um frame do RTSP.",
                    "detail": stderr[-4000:],
                    "ffmpeg": ffmpeg,
                    "transport": RTSP_TRANSPORT,
                    "url": mask_url(url),
                },
            )
            return

        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        json_response(
            self,
            200 if result.returncode == 0 else 502,
            {
                "ok": result.returncode == 0,
                "returnCode": result.returncode,
                "detail": stderr[-4000:],
                "ffmpeg": ffmpeg,
                "transport": RTSP_TRANSPORT,
                "url": mask_url(url),
            },
        )


def main():
    server = ThreadingHTTPServer((APP_HOST, APP_PORT), CameraHandler)
    thread_count = threading.active_count()
    print(f"Camera UI em http://127.0.0.1:{APP_PORT}")
    print(f"Threads iniciais: {thread_count}")
    print("Para RTSP no navegador, instale ffmpeg ou configure CAMERA_STREAM_URL com HTTP/MJPEG.")
    server.serve_forever()


if __name__ == "__main__":
    main()
