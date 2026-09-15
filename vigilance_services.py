import json
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import core_config as config
from camera_services import camera_args, get_rtsp_uri, rtsp_url_with_auth
from media_services import (
    drain_stderr,
    ffmpeg_rtsp_input_args,
    recording_status,
    start_recording,
    stop_recording,
)


WATCH_LOCK = threading.Lock()
WATCH = {
    "enabled": False,
    "thread": None,
    "process": None,
    "buffer_process": None,
    "pre_recording_paths": [],
    "last_motion_at": None,
    "last_check_at": None,
    "last_score": 0.0,
    "frames": 0,
    "motion_events": 0,
    "recording_started": False,
    "last_error": None,
}
WATCH_LOGS = deque(maxlen=500)


def _load_watch_logs():
    try:
        lines = config.WATCH_LOG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict) or not entry.get("message") or not entry.get("timestamp"):
            continue
        WATCH_LOGS.append(entry)
        if len(WATCH_LOGS) >= WATCH_LOGS.maxlen:
            break


def _persist_watch_log(entry):
    try:
        config.WATCH_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with config.WATCH_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"Nao foi possivel persistir log da vigilia: {exc}", flush=True)


_load_watch_logs()


def _buffer_command(ffmpeg, video_url, audio_url):
    segment_count = max(
        2,
        (config.MOTION_PRE_RECORD_SECONDS // config.MOTION_SEGMENT_SECONDS) + 2,
    )
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
        "-f",
        "segment",
        "-segment_time",
        str(config.MOTION_SEGMENT_SECONDS),
        "-segment_wrap",
        str(segment_count),
        "-reset_timestamps",
        "1",
        "-segment_format",
        "mp4",
        "-y",
        str(config.MOTION_BUFFER_DIR / "segment-%03d.mp4"),
    ]


def _motion_command(ffmpeg, url):
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-analyzeduration",
        "1000000",
        "-probesize",
        "32768",
        *(ffmpeg_rtsp_input_args() if urlparse(url).scheme == "rtsp" else []),
        "-i",
        url,
        "-an",
        "-map",
        "0:v:0",
        "-vf",
        f"fps={config.MOTION_FPS},scale={config.MOTION_WIDTH}:{config.MOTION_HEIGHT},format=gray",
        "-f",
        "rawvideo",
        "pipe:1",
    ]


def _motion_metrics(previous, current):
    if not previous or not current or len(previous) != len(current):
        return 0.0, 0.0
    differences = [abs(a - b) for a, b in zip(previous, current)]
    average_difference = sum(differences) / len(differences)
    changed_pixels = sum(
        1 for difference in differences if difference >= config.MOTION_PIXEL_THRESHOLD
    )
    changed_ratio = changed_pixels / len(differences)
    return average_difference, changed_ratio


def _set_state(**updates):
    with WATCH_LOCK:
        WATCH.update(updates)


def _stop_process(process):
    if not process or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=4)


def _start_buffer(ffmpeg, video_url, audio_url):
    try:
        config.MOTION_BUFFER_DIR.mkdir(parents=True, exist_ok=True)
        for path in config.MOTION_BUFFER_DIR.glob("segment-*.mp4"):
            path.unlink(missing_ok=True)
        process = subprocess.Popen(
            _buffer_command(ffmpeg, video_url, audio_url),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        drain_stderr(process)
        _set_state(buffer_process=process)
    except (OSError, ValueError) as exc:
        _set_state(last_error=f"Buffer de pre-gravacao indisponivel: {exc}")


def _capture_buffer_segments():
    cutoff = time.time() - max(1, config.MOTION_SEGMENT_SECONDS)
    paths = []
    for source in sorted(config.MOTION_BUFFER_DIR.glob("segment-*.mp4"), key=lambda item: item.stat().st_mtime):
        try:
            if source.stat().st_mtime > cutoff:
                continue
            target = config.MOTION_BUFFER_DIR / f"event-{len(paths):03d}.mp4"
            shutil.copy2(source, target)
            paths.append(target)
        except OSError:
            continue
    return paths


def _concat_files(paths, output_path):
    if not paths:
        return False
    list_path = config.MOTION_BUFFER_DIR / "concat.txt"
    lines = []
    for path in paths:
        escaped = str(Path(path).resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    try:
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        result = subprocess.run(
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                "-y",
                str(output_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        return result.returncode == 0 and output_path.exists()
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        list_path.unlink(missing_ok=True)


def _finalize_pre_recording(result):
    paths = result.get("path") and WATCH.get("pre_recording_paths", [])
    if not paths or not result.get("path"):
        return
    source = Path(result["path"])
    output = source.with_name(f"{source.stem}-complete{source.suffix}")
    if _concat_files([*paths, source], output):
        source.unlink(missing_ok=True)
        result["path"] = str(output)
        result["filename"] = output.name
    for path in paths:
        Path(path).unlink(missing_ok=True)


def _log_watch(message):
    timestamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    entry = {
        "message": message,
        "timestamp": timestamp,
        "text": f"{message}. Data: {timestamp}",
    }
    with WATCH_LOCK:
        WATCH_LOGS.appendleft(entry)
    _persist_watch_log(entry)
    print(entry["text"], flush=True)


def _start_motion_recording():
    pre_recording_paths = _capture_buffer_segments()
    args = camera_args(timeout=8.0)
    video_url = rtsp_url_with_auth(config.STREAM_URL or get_rtsp_uri(), args)
    audio_url = rtsp_url_with_auth(get_rtsp_uri(config.AUDIO_PROFILE), args)
    result = start_recording(video_url, audio_url, prefix="motion")
    if result.get("ok"):
        _set_state(
            recording_started=True,
            pre_recording_paths=pre_recording_paths,
            last_error=None,
        )
        _log_watch("Movimento Detectado, gravando")
    elif result.get("status") != 409:
        for path in pre_recording_paths:
            Path(path).unlink(missing_ok=True)
        _set_state(last_error=result.get("error"))


def _stop_motion_recording(reason="Sem Movimento, parando gravação"):
    result = stop_recording()
    if result.get("ok") or result.get("status") == 409:
        _finalize_pre_recording(result)
        _set_state(recording_started=False)
        _log_watch(reason)
    else:
        _set_state(last_error=result.get("error"))


def _watch_loop():
    previous = None
    confirmed_frames = 0
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        _set_state(enabled=False, last_error="Modo vigilia precisa de ffmpeg.")
        return

    try:
        args = camera_args(timeout=8.0)
        url = rtsp_url_with_auth(config.STREAM_URL or get_rtsp_uri(), args)
        audio_url = rtsp_url_with_auth(get_rtsp_uri(config.AUDIO_PROFILE), args)
        _start_buffer(ffmpeg, url, audio_url)
        command = _motion_command(ffmpeg, url)
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stderr_tail = drain_stderr(process)
        _set_state(process=process)

        frame_size = config.MOTION_WIDTH * config.MOTION_HEIGHT
        while True:
            with WATCH_LOCK:
                enabled = WATCH["enabled"]
            if not enabled:
                break

            frame = process.stdout.read(frame_size) if process.stdout else b""
            if len(frame) != frame_size:
                if process.poll() is not None:
                    detail = "\n".join(stderr_tail)
                    _set_state(last_error=detail[-1000:] or "ffmpeg encerrou a analise de movimento.")
                    break
                time.sleep(0.2)
                continue

            now = time.time()
            score, changed_ratio = _motion_metrics(previous, frame)
            previous = frame
            motion_candidate = (
                score >= config.MOTION_THRESHOLD
                or changed_ratio >= config.MOTION_CHANGED_RATIO
            )
            confirmed_frames = confirmed_frames + 1 if motion_candidate else 0
            motion = confirmed_frames >= config.MOTION_CONFIRM_FRAMES

            with WATCH_LOCK:
                frames = WATCH["frames"] + 1
                motion_events = WATCH["motion_events"] + (1 if motion else 0)
            updates = {
                "last_check_at": datetime.now().isoformat(timespec="seconds"),
                "last_score": round(score, 2),
                "changed_ratio": round(changed_ratio, 4),
                "frames": frames,
                "motion_events": motion_events,
            }
            if motion:
                updates["last_motion_at"] = now
            _set_state(**updates)

            with WATCH_LOCK:
                last_motion_at = WATCH["last_motion_at"]
                recording_started = WATCH["recording_started"]

            if motion and not recording_status()["running"]:
                _start_motion_recording()
            elif recording_started and last_motion_at and now - last_motion_at >= config.MOTION_IDLE_SECONDS:
                _stop_motion_recording()
    finally:
        with WATCH_LOCK:
            process = WATCH["process"]
            buffer_process = WATCH["buffer_process"]
            should_stop_recording = WATCH["recording_started"]
            WATCH["process"] = None
            WATCH["buffer_process"] = None
            WATCH["thread"] = None
            WATCH["enabled"] = False

        _stop_process(process)
        _stop_process(buffer_process)

        if should_stop_recording:
            _stop_motion_recording("Modo vigília desativado, parando gravação")


def start_watch():
    with WATCH_LOCK:
        thread = WATCH["thread"]
        if WATCH["enabled"] and thread and thread.is_alive():
            already_running = True
        else:
            already_running = False
    if already_running:
        return watch_status()

    with WATCH_LOCK:
        WATCH.update(
            {
                "enabled": True,
                "buffer_process": None,
                "pre_recording_paths": [],
                "last_motion_at": None,
                "last_check_at": None,
                "last_score": 0.0,
                "changed_ratio": 0.0,
                "frames": 0,
                "motion_events": 0,
                "recording_started": False,
                "last_error": None,
            }
        )
        thread = threading.Thread(target=_watch_loop, daemon=True)
        WATCH["thread"] = thread
        thread.start()
    return watch_status()


def stop_watch():
    with WATCH_LOCK:
        WATCH["enabled"] = False
        process = WATCH["process"]
        thread = WATCH["thread"]

    if process and process.poll() is None:
        process.terminate()

    if thread and thread.is_alive():
        thread.join(timeout=3)

    return watch_status()


def watch_status():
    with WATCH_LOCK:
        last_motion_at = WATCH["last_motion_at"]
        return {
            "enabled": WATCH["enabled"],
            "running": bool(WATCH["thread"] and WATCH["thread"].is_alive()),
            "recordingStartedByWatch": WATCH["recording_started"],
            "lastMotionAt": datetime.fromtimestamp(last_motion_at).isoformat(timespec="seconds")
            if last_motion_at
            else None,
            "lastCheckAt": WATCH["last_check_at"],
            "lastScore": WATCH["last_score"],
            "changedRatio": WATCH.get("changed_ratio", 0.0),
            "frames": WATCH["frames"],
            "motionEvents": WATCH["motion_events"],
            "idleSeconds": config.MOTION_IDLE_SECONDS,
            "threshold": config.MOTION_THRESHOLD,
            "lastError": WATCH["last_error"],
        }


def watch_logs():
    with WATCH_LOCK:
        return list(WATCH_LOGS)
