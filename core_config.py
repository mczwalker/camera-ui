import os
from pathlib import Path

from camera import DEFAULT_PROFILE


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


def refresh():
    global APP_HOST, APP_PORT, STREAM_URL, DIRECT_STREAM, RTSP_TRANSPORT, VIDEO_PROFILE
    global AUDIO_PROFILE, ENABLE_ZOOM, SNAPSHOT_DIR, RECORDING_DIR, THUMB_DIR
    global MOTION_IDLE_SECONDS, MOTION_THRESHOLD, MOTION_FPS, MOTION_WIDTH, MOTION_HEIGHT
    global MOTION_PIXEL_THRESHOLD, MOTION_CHANGED_RATIO, MOTION_CONFIRM_FRAMES
    global MOTION_PRE_RECORD_SECONDS, MOTION_SEGMENT_SECONDS, MOTION_BUFFER_DIR

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
    THUMB_DIR = ROOT / ".thumbs"
    MOTION_IDLE_SECONDS = int(os.getenv("CAMERA_MOTION_IDLE_SECONDS", "60"))
    MOTION_THRESHOLD = float(os.getenv("CAMERA_MOTION_THRESHOLD", "10"))
    MOTION_FPS = float(os.getenv("CAMERA_MOTION_FPS", "2"))
    MOTION_WIDTH = int(os.getenv("CAMERA_MOTION_WIDTH", "320"))
    MOTION_HEIGHT = int(os.getenv("CAMERA_MOTION_HEIGHT", "180"))
    MOTION_PIXEL_THRESHOLD = int(os.getenv("CAMERA_MOTION_PIXEL_THRESHOLD", "12"))
    MOTION_CHANGED_RATIO = float(os.getenv("CAMERA_MOTION_CHANGED_RATIO", "0.01"))
    MOTION_CONFIRM_FRAMES = int(os.getenv("CAMERA_MOTION_CONFIRM_FRAMES", "2"))
    MOTION_PRE_RECORD_SECONDS = int(os.getenv("CAMERA_MOTION_PRE_RECORD_SECONDS", "10"))
    MOTION_SEGMENT_SECONDS = int(os.getenv("CAMERA_MOTION_SEGMENT_SECONDS", "2"))
    MOTION_BUFFER_DIR = ROOT / ".watch_buffer"


def apply_runtime_config(updates):
    for key, value in updates.items():
        os.environ[key] = str(value)
    refresh()


refresh()
