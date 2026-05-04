# creality-hi-config

Customizations for the **Creality Hi** 3D printer running its stock firmware
(Tina/OpenWrt + Klipper + Creality-patched Moonraker + Fluidd 1.30).

The stock build is functional but missing a few pieces that matter to me:

- a plain MJPEG webcam stream that Fluidd, Obico, and Home Assistant can
  consume (stock only exposes a custom WebRTC endpoint)
- working **Spoolman** integration (the bundled Moonraker is too old to ship
  the upstream `[spoolman]` component)
- a **moonraker-obico** bridge to a self-hosted Obico server for AI
  print-failure detection
- Fluidd 1.30 dashboard webcam visibility (older Moonraker `WebCam` class
  predates the `enabled` field Fluidd now filters on)

This repo tracks only the files I own and deploy. `printer.cfg` is
intentionally not tracked because Klipper auto-modifies it (bed mesh, probe
Z-offset, PID values).

## Layout

| File | Purpose |
|---|---|
| `mjpeg_server.py` | H.264 → MJPEG bridge over Creality's `/var/run/h264_uds` socket |
| `mjpeg_server.init` | procd init script for the MJPEG bridge |
| `moonraker-obico.init` | procd init script for the self-hosted Obico bridge |
| `moonraker.conf` | Full Moonraker config including `[webcam]` and `[spoolman]` |
| `spoolman.py` | Modified upstream Moonraker spoolman component (back-ported to the older Creality-patched API) |
| `webcam.py` | Modified Moonraker webcam component (`enabled: True` for Fluidd 1.30) |
| `deploy.sh` | Idempotent uploader — md5-compares, copies what changed, restarts affected services |
| `.env.example` | Template for `.env` — printer IP, Spoolman URL, Obico URL |
| `CHANGES.md` | The detailed write-up — architecture, trade-offs, expected warnings, one-time bootstraps |

## Quick start

Requires SSH key auth as `root` on the printer.

```sh
cp .env.example .env         # one-time, edit values
./deploy.sh                  # uses PRINTER_IP from .env
./deploy.sh 192.168.1.50     # CLI arg overrides PRINTER_IP
```

`.env` (gitignored) holds `PRINTER_IP`, `SPOOLMAN_URL`, and `OBICO_URL`.
`deploy.sh` substitutes `__PRINTER_HOST__` and `__SPOOLMAN_URL__` in
`moonraker.conf` at upload time, so the same config works against any printer.

The Obico bridge needs a one-time bootstrap (download source, install Python
deps to `/mnt/UDISK`, link to your Obico server) — see **CHANGES.md §3**
before the first `deploy.sh` run.

## Read this before adopting anything

This is a working note from one printer on one firmware build. Creality ships
patched, divergent forks of Klipper and Moonraker; what works here can break
on a different revision. In particular:

- `spoolman.py` is back-ported against a specific older Moonraker API. If
  Creality ships a newer build, drop in the upstream component instead.
- The MJPEG bridge depends on `cam_app` continuing to feed `/var/run/h264_uds`.
  Unplugging a USB cam orphans the socket — see **CHANGES.md §1** for the
  trade-offs.
- Files written under `/usr/share` and `/etc` go to a 240 MB OverlayFS budget;
  larger additions belong on `/mnt/UDISK`.

Read `CHANGES.md` before deploying.

## License

`spoolman.py` and `webcam.py` are modified copies of components from
[Arksine/moonraker](https://github.com/Arksine/moonraker), distributed under
the GNU GPLv3 — original copyright notices preserved in those files. The rest
of the repo is released under the same license for consistency.
