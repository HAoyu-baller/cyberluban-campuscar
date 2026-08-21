#!/usr/bin/env python3
"""Small MJPEG server for the UVC camera used by the campus robot."""

import argparse
import json
import threading
import time
from http import server
from socketserver import ThreadingMixIn

import cv2


class Camera:
    def __init__(self, device, width, height, fps, quality):
        self.device = device
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality
        self.lock = threading.Lock()
        self.capture = None
        self.frame = None
        self.frame_time = 0.0
        self.frames = 0
        self.error = None
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.running = False
        if self.capture is not None:
            self.capture.release()
        self.thread.join(timeout=2.0)

    def _open(self):
        capture = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not capture.isOpened():
            capture.release()
            return None
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        capture.set(cv2.CAP_PROP_FPS, self.fps)
        return capture

    def _run(self):
        interval = 1.0 / max(self.fps, 1.0)
        while self.running:
            if self.capture is None:
                self.capture = self._open()
                if self.capture is None:
                    self.error = "camera_open_failed"
                    time.sleep(2.0)
                    continue
                self.error = None

            ok, frame = self.capture.read()
            if not ok or frame is None:
                self.error = "camera_read_failed"
                self.capture.release()
                self.capture = None
                time.sleep(1.0)
                continue

            ok, encoded = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.quality]
            )
            if ok:
                with self.lock:
                    self.frame = encoded.tobytes()
                    self.frame_time = time.time()
                    self.frames += 1
                self.error = None
            time.sleep(interval)

    def get_frame(self):
        with self.lock:
            return self.frame, self.frame_time

    def health(self):
        with self.lock:
            frame_time = self.frame_time
            frames = self.frames
        return {
            "device": self.device,
            "ready": frame_time > 0 and time.time() - frame_time < 3.0,
            "frames": frames,
            "last_frame_age_s": None if frame_time == 0 else round(time.time() - frame_time, 3),
            "error": self.error,
        }


class ThreadingHTTPServer(ThreadingMixIn, server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(server.BaseHTTPRequestHandler):
    camera = None

    def log_message(self, fmt, *args):
        print("camera-http: " + (fmt % args), flush=True)

    def do_GET(self):
        if self.path == "/healthz":
            body = json.dumps(self.camera.health(), separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path in ("/", "/index.html"):
            body = b"""<!doctype html>
<meta charset="utf-8"><title>Campus robot camera</title>
<style>body{margin:0;background:#111;color:#ddd;font:16px sans-serif}main{padding:16px}img{display:block;max-width:100%;height:auto}</style>
<main><div>Camera stream</div><img src="/stream.mjpg" alt="camera stream"></main>
"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path != "/stream.mjpg":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            while True:
                frame, frame_time = self.camera.get_frame()
                if frame is None or time.time() - frame_time > 3.0:
                    time.sleep(0.1)
                    continue
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(frame)).encode()
                    + b"\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
                self.wfile.flush()
                time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="/dev/video0")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--quality", type=int, default=80)
    args = parser.parse_args()

    camera = Camera(args.device, args.width, args.height, args.fps, args.quality)
    Handler.camera = camera
    camera.start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"camera-http listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        camera.stop()


if __name__ == "__main__":
    main()

