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


def apply_runtime_config(updates):
    for key, value in updates.items():
        os.environ[key] = str(value)
    refresh()


refresh()
