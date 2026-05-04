#!/usr/bin/env bash
# Deploy modified files to a Creality Hi printer.
#
# Usage:
#   ./deploy.sh [printer_ip]
#
# Configuration:
#   Reads PRINTER_IP and SPOOLMAN_URL from .env in the repo root (see
#   .env.example). A CLI arg overrides PRINTER_IP.
#
# Uploads only files whose content differs from the printer's copy, then
# restarts the affected services. Requires SSH key auth as root.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env if present. Variables already in the environment win — set -a is
# scoped so we only auto-export the .env-defined ones.
if [[ -f "${REPO_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${REPO_DIR}/.env"
    set +a
fi

PRINTER_IP="${1:-${PRINTER_IP:-}}"
SPOOLMAN_URL="${SPOOLMAN_URL:-}"

if [[ -z "${PRINTER_IP}" ]]; then
    echo "error: PRINTER_IP not set. Pass as arg or define in .env (see .env.example)." >&2
    exit 1
fi
if [[ -z "${SPOOLMAN_URL}" ]]; then
    echo "error: SPOOLMAN_URL not set in .env (see .env.example)." >&2
    exit 1
fi

SSH_TARGET="root@${PRINTER_IP}"

# Cross-platform MD5 of a local file.
local_md5() {
    if command -v md5sum >/dev/null 2>&1; then
        md5sum "$1" | awk '{print $1}'
    else
        md5 -q "$1"
    fi
}

# Returns 1-byte exit status, prints md5 of remote file (empty if missing).
remote_md5() {
    ssh "${SSH_TARGET}" "md5sum '$1' 2>/dev/null | awk '{print \$1}'"
}

# Render a repo file: substitute placeholders and write the result to a tmp
# file. Echoes the tmp file path so callers can chain it.
#
# Substitutions:
#   __PRINTER_HOST__  → $PRINTER_IP
#   __SPOOLMAN_URL__  → $SPOOLMAN_URL
#
# Uses `|` as the sed delimiter because $SPOOLMAN_URL contains slashes.
render() {
    local src="$1"
    local tmp
    tmp="$(mktemp)"
    sed -e "s|__PRINTER_HOST__|${PRINTER_IP}|g" \
        -e "s|__SPOOLMAN_URL__|${SPOOLMAN_URL}|g" \
        "${src}" > "${tmp}"
    echo "${tmp}"
}

# Upload local file if it differs from the remote copy. Sets RESTART=1 on change.
deploy_if_changed() {
    local local_path="$1"
    local remote_path="$2"
    local label="$3"

    local lmd5 rmd5
    lmd5="$(local_md5 "${local_path}")"
    rmd5="$(remote_md5 "${remote_path}")"

    if [[ "${lmd5}" == "${rmd5}" ]]; then
        echo "  ${label}: unchanged"
        RESTART=0
    else
        echo "  ${label}: uploading -> ${remote_path}"
        scp -O -q "${local_path}" "${SSH_TARGET}:${remote_path}"
        RESTART=1
    fi
}

echo ">> Deploying to ${SSH_TARGET}"

deploy_if_changed \
    "${REPO_DIR}/spoolman.py" \
    "/usr/share/moonraker/components/spoolman.py" \
    "spoolman.py"
restart_moonraker="${RESTART}"

deploy_if_changed \
    "${REPO_DIR}/webcam.py" \
    "/usr/share/moonraker/components/webcam.py" \
    "webcam.py"
[[ "${RESTART}" == "1" ]] && restart_moonraker=1

# moonraker.conf has __PRINTER_HOST__ and __SPOOLMAN_URL__ placeholders;
# render before comparing/uploading.
moonraker_conf_rendered="$(render "${REPO_DIR}/moonraker.conf")"
deploy_if_changed \
    "${moonraker_conf_rendered}" \
    "/usr/share/moonraker/moonraker.conf" \
    "moonraker.conf"
rm -f "${moonraker_conf_rendered}"
[[ "${RESTART}" == "1" ]] && restart_moonraker=1

deploy_if_changed \
    "${REPO_DIR}/mjpeg_server.py" \
    "/mnt/UDISK/mjpeg_server.py" \
    "mjpeg_server.py"
restart_mjpeg="${RESTART}"

deploy_if_changed \
    "${REPO_DIR}/mjpeg_server.init" \
    "/etc/init.d/mjpeg_server" \
    "mjpeg_server.init"
if [[ "${RESTART}" == "1" ]]; then
    ssh "${SSH_TARGET}" "chmod +x /etc/init.d/mjpeg_server && /etc/init.d/mjpeg_server enable"
    restart_mjpeg=1
fi

deploy_if_changed \
    "${REPO_DIR}/moonraker-obico.init" \
    "/etc/init.d/moonraker-obico" \
    "moonraker-obico.init"
restart_obico="${RESTART}"
if [[ "${RESTART}" == "1" ]]; then
    ssh "${SSH_TARGET}" "chmod +x /etc/init.d/moonraker-obico && /etc/init.d/moonraker-obico enable"
fi

if [[ "${restart_moonraker}" == "1" ]]; then
    echo ">> Restarting Moonraker"
    ssh "${SSH_TARGET}" "/etc/init.d/moonraker restart"
fi

if [[ "${restart_mjpeg}" == "1" ]]; then
    echo ">> Restarting mjpeg_server"
    ssh "${SSH_TARGET}" "/etc/init.d/mjpeg_server restart"
fi

if [[ "${restart_obico}" == "1" ]]; then
    if ssh "${SSH_TARGET}" "test -f /mnt/UDISK/printer_data/config/moonraker-obico.cfg"; then
        echo ">> Restarting moonraker-obico"
        ssh "${SSH_TARGET}" "/etc/init.d/moonraker-obico restart"
    else
        echo ">> moonraker-obico.cfg not found on printer — skipping start."
        echo "   Run the one-time bootstrap (see CHANGES.md, section 3) before enabling the service."
    fi
fi

if [[ "${restart_moonraker}" == "0" && "${restart_mjpeg}" == "0" && "${restart_obico}" == "0" ]]; then
    echo ">> Nothing to do."
fi

echo ">> Done."
