# Creality Hi — Configuration Changes

Printer: Creality Hi
OS: Tina/OpenWrt 21.02-SNAPSHOT (armv7l, Allwinner T113-i SoC)
Stack: Klipper + Moonraker (Creality-patched, older build) + Fluidd 1.30.0

Host- and URL-specific values (printer IP, Spoolman URL, Obico URL) are read
from `.env` in the repo root (gitignored) — see `.env.example` for the schema.

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
The full Moonraker config lives in this repo (`moonraker.conf`). It uses the
placeholders `__PRINTER_HOST__` (webcam URLs) and `__SPOOLMAN_URL__`
(`[spoolman] server`), which `deploy.sh` substitutes from `.env` at upload
time. The relevant section:

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
cp .env.example .env        # one-time, edit values
./deploy.sh                 # uses PRINTER_IP from .env
./deploy.sh 192.168.1.50    # CLI arg overrides PRINTER_IP
```

### Moonraker config
The `[spoolman]` section is in `moonraker.conf` in this repo (`__SPOOLMAN_URL__`
is substituted from `.env`):

```ini
[spoolman]
server: __SPOOLMAN_URL__
sync_rate: 5
```

---

## 3. Obico

`moonraker-obico` runs as a long-lived Python process that bridges Klipper/Moonraker
to a self-hosted Obico server (`$OBICO_URL` from `.env`). It pulls printer state from
Moonraker over WebSocket and uploads webcam snapshots from our MJPEG bridge for AI
print-failure detection.

### Why a manual install
The upstream `install.sh` assumes Debian (`apt-get`, `sudo`, `systemctl`,
`python3-virtualenv`) — none of which apply to this OpenWrt build. We do the steps
by hand: download source, install pure-Python deps with `pip --target=`, write the
config, link, register a procd service.

### One-time bootstrap

All commands run on the printer as root. ~20 MB of pure-Python deps go to
`/mnt/UDISK` (no C toolchain needed; nothing has native extensions).

```sh
# 1. Download moonraker-obico source (master = 2.2.0+)
mkdir -p /mnt/UDISK/moonraker-obico /mnt/UDISK/printer_data/logs
python3 -c "
import urllib.request
urllib.request.urlretrieve(
    'https://github.com/TheSpaghettiDetective/moonraker-obico/archive/refs/heads/master.zip',
    '/tmp/mo.zip')
"
python3 -c "
import zipfile, shutil, os
with zipfile.ZipFile('/tmp/mo.zip') as z: z.extractall('/tmp/mo-extract')
dst = '/mnt/UDISK/moonraker-obico/src'
shutil.rmtree(dst, ignore_errors=True)
shutil.move('/tmp/mo-extract/moonraker-obico-master', dst)
"
rm -rf /tmp/mo.zip /tmp/mo-extract

# 2. Restore the executable bit on bundled scripts (lost in the zip)
find /mnt/UDISK/moonraker-obico/src -name "*.sh" -exec chmod +x {} +

# 3. Install Python deps to a target dir (the system has no `ensurepip`, so a
#    venv would fail — but pip is system-wide, so --target works fine).
mkdir -p /mnt/UDISK/moonraker-obico/lib
pip3 install --no-cache-dir \
    --target=/mnt/UDISK/moonraker-obico/lib \
    -r /mnt/UDISK/moonraker-obico/src/requirements.txt

# 4. Write the config (server URL, Moonraker host, snapshot URL, log path).
#    Replace ${OBICO_URL} below with your actual server URL — heredoc is
#    intentionally quoted so $vars don't expand.
cat > /mnt/UDISK/printer_data/config/moonraker-obico.cfg <<'EOF'
[server]
url = ${OBICO_URL}

[moonraker]
host = 127.0.0.1
port = 7125

[webcam]
disable_video_streaming = False
snapshot_url = http://127.0.0.1:8081/?action=snapshot
stream_url = http://127.0.0.1:8081/?action=stream
target_fps = 15

[logging]
path = /mnt/UDISK/printer_data/logs/moonraker-obico.log
level = INFO

[tunnel]
dest_host = 127.0.0.1
dest_port = 80
dest_is_ssl = False
EOF

# 5. Link the printer to the Obico server (interactive; writes auth_token to cfg)
PYTHONPATH=/mnt/UDISK/moonraker-obico/src:/mnt/UDISK/moonraker-obico/lib \
    python3 -m moonraker_obico.link \
    -c /mnt/UDISK/printer_data/config/moonraker-obico.cfg
```

The link step prints a 6-digit verification code (e.g. `ntsmq`). On the Obico web UI,
advance the wizard to **Link Printer** and enter the code in "Switch to manual
linking". mDNS auto-discovery is unreliable across Docker bridges and home subnets,
so manual entry is the path that just works.

### Service

Once linked, `deploy.sh` pushes `moonraker-obico.init` and starts the procd service:

```sh
./deploy.sh
```

Manual control:
```sh
/etc/init.d/moonraker-obico {start,stop,restart,enable,disable}
```

Log: `/mnt/UDISK/printer_data/logs/moonraker-obico.log`

### Expected non-fatal warnings on this build
The agent emits warnings that are safe to ignore here:

| Warning | Reason |
|---|---|
| `No ffmpeg found, or ffmpeg does NOT support h264_omx/h264_v4l2m2m encoding` | The bundled probe looks for hardware H264 encoders for Janus WebRTC. Our build has neither — we use plain MJPEG snapshots instead, which is fine for failure detection. |
| `Janus not found or not configured correctly` | Same — Janus is the high-quality WebRTC path. Without it, the agent uploads JPEGs to the server at low FPS, which is what AI failure detection needs anyway. |
| `OBICO_LINK_STATUS not configured as a macro` | An optional `printer.cfg` macro for showing link status. Not added because `printer.cfg` is auto-modified by Klipper and not tracked here. |
| `error response from moonraker, ... Method not found` | Old Creality-patched Moonraker doesn't expose every API the agent expects. Non-fatal; state push and snapshot upload still work. |
| `Can not find nozzle camera. First Layer AI disabled` | Requires a separate camera mounted at the nozzle. Not present on this printer. |

### Updates
To pull a newer agent: re-run steps 1–3 of the bootstrap (the source is replaced,
deps are reinstalled, config + auth_token are preserved). Then
`/etc/init.d/moonraker-obico restart`.

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
| `moonraker-obico.init` | procd init script for the Obico bridge; deployed to `/etc/init.d/moonraker-obico`. The agent source + deps + config are not tracked — see the one-time bootstrap in section 3 |
| `webcam.py` | Modified Moonraker webcam component with `enabled: True` injected for Fluidd 1.30 compatibility; deployed to `/usr/share/moonraker/components/` |
| `moonraker.conf` | Full Moonraker config including our `[webcam]` and `[spoolman]` sections; deployed to `/usr/share/moonraker/`. Uses `__PRINTER_HOST__` and `__SPOOLMAN_URL__` placeholders — substituted by `deploy.sh` at upload |
| `deploy.sh` | Uploads changed files to the printer and restarts affected services. Reads `PRINTER_IP` and `SPOOLMAN_URL` from `.env`; accepts the printer IP as the first arg to override |
| `.env.example` | Template for `.env` — copy and fill in your printer IP, Spoolman URL, and Obico URL |
| `CHANGES.md` | This file |

The repo only tracks files we own and deploy. `printer.cfg` is intentionally
not tracked because Klipper auto-modifies it (saved bed mesh, probe Z-offset,
PID values, etc.) — the printer is the source of truth there. To inspect or
back up the current config:

```sh
scp "root@${PRINTER_IP}:/mnt/UDISK/printer_data/config/printer.cfg" .
```
