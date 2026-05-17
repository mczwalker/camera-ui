import threading
from http.server import ThreadingHTTPServer

import core_config as config
from .handlers import CameraHandler


def run():
    server = ThreadingHTTPServer((config.APP_HOST, config.APP_PORT), CameraHandler)
    print(f"Camera UI em http://127.0.0.1:{config.APP_PORT}")
    print(f"Threads iniciais: {threading.active_count()}")
    print("Para RTSP no navegador, instale ffmpeg ou configure CAMERA_STREAM_URL com HTTP/MJPEG.")
    server.serve_forever()
