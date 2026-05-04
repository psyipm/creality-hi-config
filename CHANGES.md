# Creality Hi — Configuration Changes

Printer: Creality Hi (192.168.68.37)
OS: Tina/OpenWrt 21.02-SNAPSHOT (armv7l, Allwinner T113-i SoC)
Stack: Klipper + Moonraker (Creality-patched, older build) + Fluidd 1.30.0

---

## 1. Camera Streaming (MJPEG bridge)

### Problem
Creality serves WebRTC on port 8000 using a custom signaling protocol. Fluidd,
Obico, and Home Assistant all need plain MJPEG. Trying to drive each camera
node (`/dev/video0`, `/dev/video2`) directly fails — `cam_app` holds the V4L2
lock on whichever node is active.

### Architecture
```
cam_app -i video0|video2  ──>  /var/run/h264_uds  (H264, multi-client Unix socket)
                                     │
                ┌────────────────────┼─────────────────────┐
                v                    v                     v
       webrtc_local (port 8000)   our mjpeg_server   (other consumers)
       custom WebRTC HTML        H264 → ffmpeg → MJPEG
```

Whichever camera `cam_app` currently feeds is what `/var/run/h264_uds` carries.
With only the nozzle cam present, that's the nozzle. When a USB cam is plugged
in, Creality spawns a second `cam_app` for `/dev/video2`, which **rebinds**
`/var/run/h264_uds` to the USB stream — so port 8000 and our 8081 both
automatically follow.

### Solution
A single Python MJPEG server (`mjpeg_server.py`) on port 8081:

- Connects to `/var/run/h264_uds` per request, pipes H264 → `ffmpeg` → multipart
  MJPEG (or single JPEG for snapshots).
- Serves whatever cam_app currently feeds — no fights over V4L2 locks, no
  process killing.
- Native WebRTC on port 8000 keeps working independently for the Creality app.

### Trade-offs
- **Lag**: H264→MJPEG transcode adds ~150–300 ms vs the native WebRTC at 8000.
  Acceptable for Obico AI monitoring, HA snapshots, and Fluidd dashboard glances.
  For low-latency viewing, use Creality's port 8000 WebRTC (mobile app).
- **USB unplug recovery**: when a USB cam is unplugged, `/var/run/h264_uds`
  is left bound to the now-dead USB `cam_app` (orphaned socket). Both port 8000
  and 8081 break until `cam_app` for the nozzle is restarted (or the printer
  rebooted). This is a Creality-stack quirk — not something our bridge can fix.
- **Native USB MJPEG (1920x1080) not used**: USB cam advertises native MJPEG
  but we can't open `/dev/video2` while `cam_app` holds the lock. Killing
  `cam_app` for the USB cam orphans the socket binding too.

### Files

**`mjpeg_server.py`** → `/mnt/UDISK/mjpeg_server.py`
Single-port MJPEG HTTP bridge. Endpoints:
- `GET :8081/?action=stream` — multipart MJPEG
- `GET :8081/?action=snapshot` — single JPEG

Log: `/mnt/UDISK/printer_data/logs/mjpeg_server.log`

**`mjpeg_server.init`** → `/etc/init.d/mjpeg_server`
procd init script (`START=99`, respawn enabled). Just launches the MJPEG bridge
— no `cam_app` manipulation.

```sh
/etc/init.d/mjpeg_server enable   # creates /etc/rc.d/S99mjpeg_server symlink
/etc/init.d/mjpeg_server {start,stop,restart}
```

**`moonraker.conf`** → `/usr/share/moonraker/moonraker.conf`
The full Moonraker config lives in this repo (`moonraker.conf`). Webcam URLs
use the placeholder `__PRINTER_HOST__`, which `deploy.sh` substitutes with
the printer IP at upload time. The relevant section:

```ini
[webcam camera]
location: printer
service: mjpegstreamer
target_fps: 15
stream_url: http://__PRINTER_HOST__:8081/?action=stream
snapshot_url: http://__PRINTER_HOST__:8081/?action=snapshot
flip_horizontal: False
flip_vertical: False
rotation: 0
```

### Fluidd dashboard setup (manual, per-browser)
The webcam appears in Settings → Cameras automatically. To put it on the
dashboard view: edit (pencil) icon → **Add** → **Camera** → select `camera`.
Layout is stored in browser localStorage, so each browser/profile needs this once.

### Webcam component patch
This older Moonraker `WebCam` class predates the `enabled` field. Fluidd 1.30
filters cameras by `enabled === true`, so config-managed cameras get hidden
from the dashboard widget. `webcam.py` in this repo is a copy of the printer's
component with one tweak in `WebCam.as_dict()`:

```python
d.setdefault("enabled", True)
```

Deployed alongside `spoolman.py` to `/usr/share/moonraker/components/`.

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
and calls `_handle_status_update(result, eventtime)` directly (synchronous,
two positional args — same signature the upstream subscribe callback would use).
Any stale task is cancelled on Klippy reconnect to prevent accumulation.

Verified working: spool weight decrements and "Last used" timestamp updates in
the Spoolman web UI during an actual print.

### Deployment
Use `deploy.sh` from the repo root — uploads any changed file and restarts the
affected services:
```sh
./deploy.sh                 # uses default IP 192.168.68.37
./deploy.sh 10.0.0.42       # custom IP
```

### Moonraker config
The `[spoolman]` section is in `moonraker.conf` in this repo:

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
| `mjpeg_server.py` | MJPEG bridge for the Creality H264 socket; deployed to `/mnt/UDISK/` |
| `mjpeg_server.init` | procd init script for the MJPEG bridge; deployed to `/etc/init.d/mjpeg_server` |
| `webcam.py` | Modified Moonraker webcam component with `enabled: True` injected for Fluidd 1.30 compatibility; deployed to `/usr/share/moonraker/components/` |
| `moonraker.conf` | Full Moonraker config including our `[webcam]` and `[spoolman]` sections; deployed to `/usr/share/moonraker/`. Webcam URLs use `__PRINTER_HOST__` placeholder — substituted by `deploy.sh` at upload |
| `deploy.sh` | Uploads changed files to the printer and restarts affected services. Accepts the printer IP as the first arg (default `192.168.68.37`); also substitutes `__PRINTER_HOST__` in `moonraker.conf` |
| `printer.cfg` | Reference copy of the live Klipper config from the printer (includes Klipper's autosaved bed mesh + probe data; never deployed back) |
| `CHANGES.md` | This file |
