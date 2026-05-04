# Creality Hi — Configuration Changes

Printer: Creality Hi (192.168.68.37)
OS: Tina/OpenWrt 21.02-SNAPSHOT (armv7l, Allwinner T113-i SoC)
Stack: Klipper + Moonraker (Creality-patched, older build) + Fluidd 1.30.0

---

## 1. Camera Streaming (MJPEG)

### Problem
The printer uses a proprietary `webrtc_local` binary serving WebRTC on port 8000.
Fluidd has no native support for Creality's custom WebRTC signaling format.
Both cameras (`/dev/video0` nozzle, `/dev/video2` USB) were exclusively locked by
`cam_app` processes; ffmpeg could not open them concurrently.

### Architecture discovered
```
cam_app -i /dev/video0  →  listens on /var/run/h264_uds (Unix socket, H264)
cam_app -i /dev/video2  →  locks /dev/video2, no accessible socket path
         ↓                         ↓
webrtc_local (PID 3347) ←  connects to /var/run/h264_uds during active print
webrtc_local (PID 2309) ←  (USB cam pair, orphaned socket - no path binding)
         ↓
   port 8000 (WebRTC, HTML page, round-robin between both instances)
```

Key findings:
- `cam_app` (nozzle) accepts **multiple simultaneous clients** on `/var/run/h264_uds`
- The nozzle cam socket is accessible 24/7; webrtc_local only connects during prints
- The USB cam socket (inode 4189) loses its filesystem path because the nozzle cam_app
  starts second and rebinds `/var/run/h264_uds`, making the USB socket unreachable by path
- `ffmpeg` is available on the system

### Solution
- **Nozzle cam (port 8081)**: Python MJPEG server connects to `/var/run/h264_uds`,
  pipes H264 → ffmpeg → multipart MJPEG over HTTP. Non-destructive — `cam_app` keeps
  running, timelapse and AI monitoring for the nozzle cam are unaffected.
- **USB cam (port 8082)**: `cam_app` for `/dev/video2` is killed at startup (it has
  no webrtc client connected and its H264 socket is unreachable anyway), freeing
  `/dev/video2` for ffmpeg to capture directly as MJPEG.

### Files created/modified on printer

**New file: `/mnt/UDISK/mjpeg_server.py`**
Python MJPEG HTTP server. Serves both cameras:
- `:8081/?action=stream` — nozzle cam (H264 socket → ffmpeg)
- `:8081/?action=snapshot` — nozzle cam single frame
- `:8082/?action=stream` — USB cam (V4L2 → ffmpeg)
- `:8082/?action=snapshot` — USB cam single frame

Log: `/mnt/UDISK/printer_data/logs/mjpeg_server.log`

**New file: `/etc/init.d/mjpeg_server`**
procd init script (START=99, after Moonraker). Kills any running `cam_app` instance
holding `/dev/video2` before starting the MJPEG server. Has `respawn` configured.

Enable/disable:
```sh
/etc/init.d/mjpeg_server enable   # creates /etc/rc.d/S99mjpeg_server symlink
/etc/init.d/mjpeg_server disable
/etc/init.d/mjpeg_server start
/etc/init.d/mjpeg_server stop
```

**Modified: `/usr/share/moonraker/moonraker.conf`**
Added two `[webcam]` sections:

```ini
[webcam usb_camera]
location: printer
service: mjpegstreamer
target_fps: 15
stream_url: http://192.168.68.37:8082/?action=stream
snapshot_url: http://192.168.68.37:8082/?action=snapshot
flip_horizontal: False
flip_vertical: False
rotation: 0

[webcam nozzle_camera]
location: toolhead
service: mjpegstreamer
target_fps: 15
stream_url: http://192.168.68.37:8081/?action=stream
snapshot_url: http://192.168.68.37:8081/?action=snapshot
flip_horizontal: False
flip_vertical: False
rotation: 0
```

### Fluidd dashboard setup (manual, per-browser)
Cameras registered in Moonraker appear in Settings → Cameras without warning icons.
To show them on the dashboard: pencil icon → Add → Camera → select each camera.

### Trade-offs
- Creality's native WebRTC stream on port 8000 continues to work for the nozzle cam
  (during prints), but the USB cam no longer has its own WebRTC stream.
- USB cam timelapse (was writing to `/mnt/UDISK/timelapse/main_output.h264` via
  `cam_app`) is no longer recorded. Nozzle cam timelapse is unaffected.
- AI monitoring (object detection via `/tmp/shm/main_ai_image`) for the USB cam stops.
  Nozzle cam AI monitoring is unaffected.

---

## 2. Spoolman Integration

### Problem
Moonraker on this printer is a Creality-patched older build that predates the
`[spoolman]` component (added to upstream Moonraker in October 2023) and has
several API differences from the version spoolman.py was written against.

### Solution
`spoolman.py` in this repo is a modified copy of the upstream component
(`Arksine/moonraker`, commit at time of setup). It is deployed directly to the
printer — no patch scripts required.

**Modifications vs upstream:**

| Issue | Fix |
|---|---|
| `from ..common import RequestType, HistoryFieldData` — `common` module missing | Inlined `RequestType` (enum.Flag) and `HistoryFieldData` class definitions directly |
| `from ..utils import json_wrapper as jsonw` — `json_wrapper` missing from this `utils.py` | Replaced with `import json as jsonw` |
| `register_endpoint(..., RequestType.GET \| RequestType.POST, ...)` — old API takes string lists | Replaced with `["GET", "POST"]` etc. in `_register_endpoints` |
| `web_request.get_request_type()` — method does not exist; old API uses `get_action()` | Replaced with `web_request.get_action() == "POST"` |
| `history.register_auxiliary_field(...)` — method absent in this history component | Wrapped in `if hasattr(history, 'register_auxiliary_field')` |
| `announcements.register_feed("spoolman")` — method absent in this announcements component | Wrapped in `if hasattr(announcements, 'register_feed')` |
| `subscribe_objects(..., callback, {})` — old API ignores the callback argument | Wrapped in `try/except TypeError`; fallback launches a polling task (see below) |
| `self.spool_history.tracker.update()` — `tracker` attr set by `register_auxiliary_field`, which is absent here | Added `_DummyTracker` stub with a no-op `update()` to `HistoryFieldData` |

**Real-time filament tracking via polling:**

`subscribe_objects()` in this Moonraker version silently ignores the callback
argument, so `_handle_status_update()` would never be called during a print.
The `except TypeError` fallback instead launches `_poll_extruder_position()`,
an asyncio task that polls `query_objects()` at `sync_rate_seconds` intervals
and feeds results directly into `_handle_status_update()`. Any stale task is
cancelled on Klippy reconnect to prevent accumulation.

### Deployment
```sh
scp spoolman.py root@192.168.68.37:/usr/share/moonraker/components/spoolman.py
/etc/init.d/moonraker restart
```

### Moonraker config
**Modified: `/usr/share/moonraker/moonraker.conf`**
```ini
[spoolman]
server: http://spoolman.home
sync_rate: 5
```

---

## 3. Obico

**Status: Not implemented.**

Obico requires the `moonraker-obico` plugin. Without `git` or `wget` on the printer,
installation requires downloading the zip archive via Python `urllib.request` and
extracting it to `/mnt/UDISK/`. This was not attempted during this session.

Reference: https://obico.io/docs/user-guides/klipper-setup/

Suggested approach when implementing:
```python
import urllib.request, zipfile, os

urllib.request.urlretrieve(
    'https://github.com/obico/moonraker-obico/archive/refs/heads/main.zip',
    '/mnt/UDISK/moonraker-obico.zip'
)
with zipfile.ZipFile('/mnt/UDISK/moonraker-obico.zip') as z:
    z.extractall('/mnt/UDISK/')
os.rename('/mnt/UDISK/moonraker-obico-main', '/mnt/UDISK/moonraker-obico')
```

Then follow the moonraker-obico install script logic manually (link plugin, configure
Moonraker, create `[obico]` section pointing to `http://obico.home`).

---

## Filesystem overlay note

The printer uses an OverlayFS setup:
- `/dev/root` → `/rom` — read-only squashfs (factory firmware, 115MB full)
- `/dev/mmcblk0p10` → `/overlay` — writable ext4 (240MB, ~233MB free)
- `/dev/by-name/UDISK` → `/mnt/UDISK` — writable ext4 (6.2GB, ~5.6GB free)

Files written to `/usr/share/`, `/etc/` etc. go to the overlay (240MB budget).
Larger additions (scripts, binaries, plugins) should go to `/mnt/UDISK/`.

The `moonraker.conf` and `spoolman.py` are on the overlay. The `mjpeg_server.py`
is on UDISK. The init script `/etc/init.d/mjpeg_server` is on the overlay.

---

## Files in this repository

| File | Purpose |
|---|---|
| `spoolman.py` | Modified upstream Moonraker spoolman component; deployed to `/usr/share/moonraker/components/` |
| `mjpeg_server.py` | MJPEG streaming server for both cameras; deployed to `/mnt/UDISK/` |
| `printer.cfg` | Klipper printer configuration (unmodified factory copy) |
| `CHANGES.md` | This file |
