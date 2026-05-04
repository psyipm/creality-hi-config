#!/usr/bin/env python3
"""
MJPEG bridge for Creality Hi cameras.

A single ffmpeg pipeline keeps decoding H264 from cam_app's Unix socket
into MJPEG frames; HTTP requests pull from the latest cached frame, so
snapshots return instantly and streams start with the next frame.

Endpoints:
  GET /?action=stream    multipart MJPEG stream
  GET /?action=snapshot  single JPEG (latest cached frame)

The native WebRTC stream on port 8000 is independent; this bridge only
exists for clients that need MJPEG (Fluidd, Obico, Home Assistant).
"""

import logging
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

H264_SOCKET    = '/var/run/h264_uds'
PORT           = 8081
FFMPEG_FPS     = '15'
FFMPEG_QUALITY = '5'   # 1=best, 31=worst
RECONNECT_DELAY = 2.0  # seconds between reconnect attempts on socket failure
LOG_FILE       = '/mnt/UDISK/printer_data/logs/mjpeg_server.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()],
)
log = logging.getLogger(__name__)

JPEG_BOUNDARY = b'--frame'


class FrameProvider:
    """Persistent decoder: H264 socket -> ffmpeg -> latest JPEG frame.

    Multiple HTTP handlers can read concurrently — readers either grab the
    latest cached frame (snapshot) or block on the condition variable for
    the next frame (stream).
    """

    def __init__(self):
        self._latest = None
        self._frame_id = 0
        self._cond = threading.Condition()
        self._stop = threading.Event()
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _open_socket(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(H264_SOCKET)
        return s

    def _spawn_ffmpeg(self):
        return subprocess.Popen(
            ['ffmpeg', '-loglevel', 'error',
             '-f', 'h264', '-i', 'pipe:0',
             '-vf', f'fps={FFMPEG_FPS}',
             '-f', 'image2pipe', '-vcodec', 'mjpeg',
             '-q:v', FFMPEG_QUALITY, 'pipe:1'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _pump_socket_to_ffmpeg(self, sock, proc):
        """Background thread: copies bytes from cam_app socket to ffmpeg stdin."""
        try:
            while not self._stop.is_set():
                chunk = sock.recv(32768)
                if not chunk:
                    return
                proc.stdin.write(chunk)
        except Exception as exc:
            log.debug('pump ended: %s', exc)
        finally:
            try: proc.stdin.close()
            except Exception: pass

    def _read_jpeg_frames(self, proc):
        """Generator: yields complete JPEGs from ffmpeg's image2pipe output."""
        buf = b''
        while not self._stop.is_set():
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

    def _run_loop(self):
        """Open socket + ffmpeg, stream frames into the cache; reconnect on failure."""
        while not self._stop.is_set():
            sock = proc = None
            try:
                sock = self._open_socket()
                proc = self._spawn_ffmpeg()
                threading.Thread(
                    target=self._pump_socket_to_ffmpeg,
                    args=(sock, proc),
                    daemon=True,
                ).start()
                log.info('decoder pipeline started')
                for jpg in self._read_jpeg_frames(proc):
                    with self._cond:
                        self._latest = jpg
                        self._frame_id += 1
                        self._cond.notify_all()
                log.warning('decoder pipeline ended; reconnecting')
            except FileNotFoundError:
                log.warning('%s not present; will retry', H264_SOCKET)
            except ConnectionRefusedError:
                log.warning('%s connection refused; will retry', H264_SOCKET)
            except Exception:
                log.exception('decoder pipeline crashed')
            finally:
                if sock is not None:
                    try: sock.close()
                    except Exception: pass
                if proc is not None:
                    try: proc.kill()
                    except Exception: pass
            time.sleep(RECONNECT_DELAY)

    def latest_frame(self, wait_timeout=5.0):
        """Return the most recent JPEG, blocking up to wait_timeout for the first frame."""
        with self._cond:
            if self._latest is None:
                self._cond.wait(timeout=wait_timeout)
            return self._latest

    def stream_frames(self):
        """Generator: yields a fresh JPEG each time a new frame arrives."""
        last_id = -1
        while True:
            with self._cond:
                while last_id == self._frame_id:
                    if not self._cond.wait(timeout=10.0):
                        return  # timeout — caller decides whether to retry
                last_id = self._frame_id
                frame = self._latest
            yield frame


provider = FrameProvider()


class CameraHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/?action=snapshot':
            self._snapshot()
        else:
            self._stream()

    def _snapshot(self):
        jpg = provider.latest_frame()
        if not jpg:
            self.send_error(503, 'Camera unavailable')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(jpg)))
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(jpg)

    def _stream(self):
        if provider.latest_frame(wait_timeout=2.0) is None:
            self.send_error(503, 'Camera unavailable')
            return
        self.send_response(200)
        self.send_header('Content-Type',
                         'multipart/x-mixed-replace; boundary=frame')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        try:
            for jpg in provider.stream_frames():
                self.wfile.write(JPEG_BOUNDARY +
                                 b'\r\nContent-Type: image/jpeg\r\n\r\n' +
                                 jpg + b'\r\n')
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):
        pass  # suppress per-request logs


class ThreadingHTTPServer(HTTPServer):
    """One thread per request so concurrent stream + snapshot don't block."""
    def process_request(self, request, client_address):
        threading.Thread(
            target=self._handle_one,
            args=(request, client_address),
            daemon=True,
        ).start()

    def _handle_one(self, request, client_address):
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
