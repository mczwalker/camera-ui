import json
import shutil
import subprocess
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
from requests.auth import HTTPDigestAuth

import core_config as config
from camera import DIRECTIONS, clamp, move_raw, stop_raw
from camera_services import (
    basic_device_info,
    basic_profiles_info,
    camera_args,
    camera_identification,
    camera_supports_zoom,
    get_rtsp_uri,
    mask_url,
    rtsp_url_with_auth,
)
from media_services import (
    RECORDING,
    RECORDING_LOCK,
    build_ffmpeg_audio_command,
    build_ffmpeg_record_command,
    build_ffmpeg_snapshot_command,
    build_ffmpeg_stream_command,
    build_ffmpeg_test_command,
    drain_stderr,
    media_items,
    recording_status,
    stream_kind,
)
from network_services import network_status
from .responses import json_response

CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
ROOT = config.ROOT


def refresh_runtime_aliases():
    config.refresh()


def setup_payload():
    args = camera_args(timeout=5.0)
    profiles = basic_profiles_info()
    return {
        "camera": {
            "ip": args.host,
            "port": args.port,
            "user": args.user,
            "profile": args.profile,
            "videoProfile": config.VIDEO_PROFILE or args.profile,
            "audioProfile": config.AUDIO_PROFILE,
            "rtspTransport": config.RTSP_TRANSPORT,
            "enableZoom": config.ENABLE_ZOOM,
            "uiHost": config.APP_HOST,
            "uiPort": config.APP_PORT,
            "snapshotDir": str(config.SNAPSHOT_DIR),
            "recordingDir": str(config.RECORDING_DIR),
            "streamUrl": config.STREAM_URL,
            "directStream": config.DIRECT_STREAM,
        },
        "profiles": profiles,
        "device": basic_device_info(),
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
            url = config.STREAM_URL or get_rtsp_uri()
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
                    "videoProfile": config.VIDEO_PROFILE or args.profile,
                    "audioProfile": config.AUDIO_PROFILE,
                    "streamUrl": url,
                    "streamKind": kind,
                    "directStream": config.DIRECT_STREAM and kind == "http",
                    "ffmpegAvailable": ffmpeg_path is not None,
                    "ffmpegPath": ffmpeg_path,
                    "rtspTransport": config.RTSP_TRANSPORT,
                    "hasBrowserStream": kind == "http" or (kind == "rtsp" and ffmpeg_path is not None),
                    "hasAudioStream": kind == "rtsp" and ffmpeg_path is not None,
                    "supportsZoom": supports_zoom,
                    "snapshotDir": str(config.SNAPSHOT_DIR),
                    "recordingDir": str(config.RECORDING_DIR),
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
            payload = camera_identification(network_status())
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

        path = config.SNAPSHOT_DIR / filename
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

        path = config.RECORDING_DIR / filename
        if path.suffix.lower() != ".mp4" or not path.is_file():
            json_response(self, 404, {"ok": False, "error": "Arquivo nao encontrado"})
            return

        try:
            path.unlink()
            thumb = config.THUMB_DIR / f"{path.stem}.jpg"
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
        url = rtsp_url_with_auth(config.STREAM_URL or get_rtsp_uri(), args)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"snapshot-{timestamp}.jpg"
        output_path = config.SNAPSHOT_DIR / filename

        try:
            config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
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
            config.save_dotenv(updates)
            config.apply_runtime_config(updates)
            refresh_runtime_aliases()
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
        video_url = rtsp_url_with_auth(config.STREAM_URL or get_rtsp_uri(), args)
        audio_url = rtsp_url_with_auth(get_rtsp_uri(config.AUDIO_PROFILE), args)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"recording-{timestamp}.mp4"
        output_path = config.RECORDING_DIR / filename

        try:
            config.RECORDING_DIR.mkdir(parents=True, exist_ok=True)
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
            base_dir = config.THUMB_DIR
            content_type = "image/jpeg"
            allowed_suffix = ".jpg"
        elif kind == "snapshots":
            base_dir = config.SNAPSHOT_DIR
            content_type = "image/jpeg"
            allowed_suffix = ".jpg"
        elif kind == "recordings":
            base_dir = config.RECORDING_DIR
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
        url = config.STREAM_URL or get_rtsp_uri()
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
        print(f"Iniciando ffmpeg RTSP: {mask_url(url)} transport={config.RTSP_TRANSPORT}")

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
        url = rtsp_url_with_auth(get_rtsp_uri(config.AUDIO_PROFILE), args)
        command = build_ffmpeg_audio_command(ffmpeg, url)
        print(f"Iniciando ffmpeg audio: {mask_url(url)} transport={config.RTSP_TRANSPORT}")

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
        url = config.STREAM_URL or get_rtsp_uri()
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
                    "transport": config.RTSP_TRANSPORT,
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
                "transport": config.RTSP_TRANSPORT,
                "url": mask_url(url),
            },
        )
