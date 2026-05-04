#!/usr/bin/env python3
"""
MJPEG bridge for Creality Hi cameras.

Reads H264 from the cam_app Unix socket and re-emits MJPEG over HTTP for
clients that don't speak Creality's custom WebRTC (Fluidd, Obico, Home
Assistant). Whichever camera cam_app currently has bound to /var/run/h264_uds
is what gets served — typically the USB cam when one is plugged in,
otherwise the built-in nozzle cam.

Endpoints:
  GET /?action=stream    multipart MJPEG stream
  GET /?action=snapshot  single JPEG

The native WebRTC stream on port 8000 keeps working independently.
"""

import logging
import signal
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

H264_SOCKET   = '/var/run/h264_uds'
PORT          = 8081
FFMPEG_FPS    = '15'
FFMPEG_QUALITY = '5'   # 1=best, 31=worst
LOG_FILE      = '/mnt/UDISK/printer_data/logs/mjpeg_server.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

JPEG_BOUNDARY = b'--frame'


def read_jpeg_frames(proc):
    """Yield JPEG byte blobs from an ffmpeg image2pipe stdout."""
    buf = b''
    while True:
        chunk = proc.stdout.read(8192)
        if not chunk:
            return
        buf += chunk
        while True:
            start = buf.find(b'\xff\xd8')
            end = buf.find(b'\xff\xd9', start + 2)
            if start == -1 or end == -1:
                break
            yield buf[start:end + 2]
            buf = buf[end + 2:]


def launch_ffmpeg_h264_to_mjpeg(unix_sock):
    """Spawn ffmpeg reading H264 from stdin (fed by unix_sock) → MJPEG to stdout."""
    proc = subprocess.Popen(
        ['ffmpeg', '-loglevel', 'error',
         '-f', 'h264', '-i', 'pipe:0',
         '-vf', f'fps={FFMPEG_FPS}',
         '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', FFMPEG_QUALITY, 'pipe:1'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    def pump():
        try:
            while True:
                chunk = unix_sock.recv(32768)
                if not chunk:
                    return
                proc.stdin.write(chunk)
        except Exception:
            pass
        finally:
            try: proc.stdin.close()
            except Exception: pass
            try: unix_sock.close()
            except Exception: pass

    threading.Thread(target=pump, daemon=True).start()
    return proc


def open_h264_socket():
    """Connect to cam_app's Unix socket. Returns socket or None on failure."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(H264_SOCKET)
        return sock
    except Exception as exc:
        log.warning('cannot connect to %s: %s', H264_SOCKET, exc)
        sock.close()
        return None


class CameraHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/?action=snapshot':
            self._snapshot()
        else:
            self._stream()

    def _stream(self):
        sock = open_h264_socket()
        if sock is None:
            self.send_error(503, 'Camera unavailable')
            return
        proc = launch_ffmpeg_h264_to_mjpeg(sock)
        self.send_response(200)
        self.send_header('Content-Type',
                         'multipart/x-mixed-replace; boundary=frame')
        self.end_headers()
        try:
            for jpg in read_jpeg_frames(proc):
                self.wfile.write(JPEG_BOUNDARY +
                                 b'\r\nContent-Type: image/jpeg\r\n\r\n' +
                                 jpg + b'\r\n')
        except Exception:
            pass
        finally:
            proc.kill()

    def _snapshot(self):
        sock = open_h264_socket()
        if sock is None:
            self.send_error(503, 'Camera unavailable')
            return
        proc = launch_ffmpeg_h264_to_mjpeg(sock)
        try:
            jpg = next(read_jpeg_frames(proc), b'')
        finally:
            proc.kill()
        if not jpg:
            self.send_error(503, 'No frame received')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(jpg)))
        self.end_headers()
        self.wfile.write(jpg)

    def log_message(self, fmt, *args):
        pass  # suppress per-request access logs


class ThreadingHTTPServer(HTTPServer):
    """One thread per request so simultaneous stream + snapshot don't block."""
    def process_request(self, request, client_address):
        threading.Thread(
            target=self._process,
            args=(request, client_address),
            daemon=True,
        ).start()

    def _process(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        finally:
            self.shutdown_request(request)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    server = ThreadingHTTPServer(('0.0.0.0', PORT), CameraHandler)
    log.info('MJPEG bridge listening on :%d (source: %s)', PORT, H264_SOCKET)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info('shutting down')
