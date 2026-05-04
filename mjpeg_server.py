#!/usr/bin/env python3
"""
MJPEG streaming server for Creality Hi cameras.

Camera A (nozzle, primary): reads H264 from cam_app via Unix socket
Camera B (USB, secondary):  reads from V4L2 device directly via ffmpeg
"""

import os
import socket
import subprocess
import threading
import logging
import signal
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('/mnt/UDISK/printer_data/logs/mjpeg_server.log'),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────
NOZZLE_SOCKET   = '/var/run/h264_uds'   # cam_app Unix socket (nozzle cam)
USB_DEVICE      = '/dev/video2'          # V4L2 device (USB cam)

NOZZLE_PORT     = 8081
USB_PORT        = 8082

FFMPEG_QUALITY  = '5'   # JPEG quality (1=best, 31=worst)
FFMPEG_FPS      = '15'
FFMPEG_SCALE    = '1280:720'
# ─────────────────────────────────────────────────────────────────────────────

BOUNDARY = b'--frame'


def read_frames_from_ffmpeg(proc):
    """Generator: yields JPEG bytes from ffmpeg image2pipe output."""
    buf = b''
    try:
        while True:
            chunk = proc.stdout.read(8192)
            if not chunk:
                break
            buf += chunk
            while True:
                s = buf.find(b'\xff\xd8')
                e = buf.find(b'\xff\xd9', s + 2)
                if s == -1 or e == -1:
                    break
                yield buf[s:e + 2]
                buf = buf[e + 2:]
    except Exception:
        pass


def launch_ffmpeg_v4l2(device):
    """Start ffmpeg reading directly from a V4L2 device."""
    cmd = [
        'ffmpeg', '-loglevel', 'error',
        '-f', 'v4l2', '-i', device,
        '-vf', f'fps={FFMPEG_FPS},scale={FFMPEG_SCALE}',
        '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', FFMPEG_QUALITY, 'pipe:1',
    ]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def launch_ffmpeg_h264_pipe(data_socket):
    """Start ffmpeg reading H264 from stdin (fed from a Unix socket)."""
    cmd = [
        'ffmpeg', '-loglevel', 'error',
        '-f', 'h264', '-i', 'pipe:0',
        '-vf', f'fps={FFMPEG_FPS}',
        '-f', 'image2pipe', '-vcodec', 'mjpeg', '-q:v', FFMPEG_QUALITY, 'pipe:1',
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def feed():
        try:
            while True:
                chunk = data_socket.recv(32768)
                if not chunk:
                    break
                proc.stdin.write(chunk)
        except Exception:
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                data_socket.close()
            except Exception:
                pass

    threading.Thread(target=feed, daemon=True).start()
    return proc


class NozzleCamHandler(BaseHTTPRequestHandler):
    """Serves the nozzle cam via H264 Unix socket → ffmpeg → MJPEG."""

    def do_GET(self):
        if self.path == '/?action=snapshot':
            self._snapshot()
        else:
            self._stream()

    def _stream(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(NOZZLE_SOCKET)
        except Exception as exc:
            log.error('Cannot connect to nozzle socket: %s', exc)
            self.send_error(503, 'Nozzle camera unavailable')
            return

        proc = launch_ffmpeg_h264_pipe(sock)
        self.send_response(200)
        self.send_header('Content-Type',
                         'multipart/x-mixed-replace; boundary=frame')
        self.end_headers()
        try:
            for jpg in read_frames_from_ffmpeg(proc):
                self.wfile.write(
                    BOUNDARY + b'\r\nContent-Type: image/jpeg\r\n\r\n'
                    + jpg + b'\r\n'
                )
        except Exception:
            pass
        finally:
            proc.kill()
            sock.close()

    def _snapshot(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(NOZZLE_SOCKET)
        except Exception as exc:
            log.error('Cannot connect to nozzle socket: %s', exc)
            self.send_error(503, 'Nozzle camera unavailable')
            return

        proc = launch_ffmpeg_h264_pipe(sock)
        jpg = b''
        for frame in read_frames_from_ffmpeg(proc):
            jpg = frame
            break
        proc.kill()
        sock.close()

        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(jpg)))
        self.end_headers()
        self.wfile.write(jpg)

    def log_message(self, fmt, *args):
        pass  # suppress per-request logs


class UsbCamHandler(BaseHTTPRequestHandler):
    """Serves the USB cam via V4L2 → ffmpeg → MJPEG."""

    def do_GET(self):
        if self.path == '/?action=snapshot':
            self._snapshot()
        else:
            self._stream()

    def _stream(self):
        if not os.path.exists(USB_DEVICE):
            self.send_error(503, 'USB camera not connected')
            return

        proc = launch_ffmpeg_v4l2(USB_DEVICE)
        self.send_response(200)
        self.send_header('Content-Type',
                         'multipart/x-mixed-replace; boundary=frame')
        self.end_headers()
        try:
            for jpg in read_frames_from_ffmpeg(proc):
                self.wfile.write(
                    BOUNDARY + b'\r\nContent-Type: image/jpeg\r\n\r\n'
                    + jpg + b'\r\n'
                )
        except Exception:
            pass
        finally:
            proc.kill()

    def _snapshot(self):
        if not os.path.exists(USB_DEVICE):
            self.send_error(503, 'USB camera not connected')
            return

        proc = launch_ffmpeg_v4l2(USB_DEVICE)
        jpg = b''
        for frame in read_frames_from_ffmpeg(proc):
            jpg = frame
            break
        proc.kill()

        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(jpg)))
        self.end_headers()
        self.wfile.write(jpg)

    def log_message(self, fmt, *args):
        pass


def serve(port, handler_class):
    server = HTTPServer(('0.0.0.0', port), handler_class)
    log.info('Camera server on port %d', port)
    server.serve_forever()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    threads = [
        threading.Thread(target=serve, args=(NOZZLE_PORT, NozzleCamHandler), daemon=True),
        threading.Thread(target=serve, args=(USB_PORT,    UsbCamHandler),    daemon=True),
    ]
    for t in threads:
        t.start()

    log.info('MJPEG server started (nozzle=%d, usb=%d)', NOZZLE_PORT, USB_PORT)
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        log.info('Shutting down')
