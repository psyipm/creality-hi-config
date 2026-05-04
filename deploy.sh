#!/usr/bin/env bash
# Deploy modified files to a Creality Hi printer.
#
# Usage:
#   ./deploy.sh [printer_ip]
#
# Default printer IP: 192.168.68.37
#
# Uploads only files whose content differs from the printer's copy, then
# restarts the affected services. Requires SSH key auth as root.

set -euo pipefail

PRINTER_IP="${1:-192.168.68.37}"
SSH_TARGET="root@${PRINTER_IP}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

if [[ "${restart_moonraker}" == "1" ]]; then
    echo ">> Restarting Moonraker"
    ssh "${SSH_TARGET}" "/etc/init.d/moonraker restart"
fi

if [[ "${restart_mjpeg}" == "1" ]]; then
    echo ">> Restarting mjpeg_server"
    ssh "${SSH_TARGET}" "/etc/init.d/mjpeg_server restart"
fi

if [[ "${restart_moonraker}" == "0" && "${restart_mjpeg}" == "0" ]]; then
    echo ">> Nothing to do."
fi

echo ">> Done."
