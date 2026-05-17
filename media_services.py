import shutil
import subprocess
import threading
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import core_config as config


RECORDING_LOCK = threading.Lock()
RECORDING = {
    "process": None,
    "path": None,
    "filename": None,
    "started_at": None,
    "stderr": None,
}


def stream_kind(url):
    if not url:
        return "rtsp"
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return "http"
    if parsed.scheme == "rtsp":
        return "rtsp"
    return "unknown"


def has_ffmpeg():
    return shutil.which("ffmpeg") is not None


def ffmpeg_rtsp_input_args():
    if config.RTSP_TRANSPORT in {"tcp", "udp", "udp_multicast", "http", "https"}:
        return ["-rtsp_transport", config.RTSP_TRANSPORT]
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


def recording_thumb(path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    try:
        config.THUMB_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    thumb_name = f"{path.stem}.jpg"
    thumb_path = config.THUMB_DIR / thumb_name
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
        directory = config.SNAPSHOT_DIR
        pattern = "*.jpg"
    elif kind == "recordings":
        directory = config.RECORDING_DIR
        pattern = "*.mp4"
    else:
        return None

    try:
        files = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime, reverse=True)
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


def start_recording_process(output_path, command):
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    stderr_tail = drain_stderr(process)
    with RECORDING_LOCK:
        RECORDING.update(
            {
                "process": process,
                "path": output_path,
                "filename": output_path.name,
                "started_at": datetime.now().isoformat(timespec="seconds"),
                "stderr": stderr_tail,
            }
        )
    return process, stderr_tail


def clear_recording_state(output_path=None, filename=None):
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
