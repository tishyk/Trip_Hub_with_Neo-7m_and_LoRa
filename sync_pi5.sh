#!/usr/bin/env bash
# Sync the Pi 5 hub: push Hub_Server / Hub_Server_Firmware / Trip_Hub,
# flash Pico A via mpremote, (re)start both Hub_Server and Trip_Hub.
#
# Usage:
#   ./sync_pi5.sh                      # full deploy
#   ./sync_pi5.sh --no-flash           # skip Pico A flash
#   ./sync_pi5.sh --no-restart         # skip service restart (no Hub/Trip)
#   ./sync_pi5.sh --only Hub_Server    # sync just one project (repeatable)
#
# Services managed (each in its own tmux session):
#   - hub_server : python3 hub.py --server http://localhost:5000
#                  (USB bridge to Pico A; POSTs GPS to Trip_Hub)
#   - trip_hub   : python3 trip_server.py  (Flask map UI on :5000)
#
# Run from Git Bash / WSL on Windows, or any bash. Requires:
#   - a host alias in ~/.ssh/config (HostName <pi-ip>, User <pi-user>),
#     selected via $PI_HOST below (defaults to 'pi5')
#   - pubkey already authorized on the Pi
#   - mpremote installed on the Pi (~/.local/bin/mpremote, on PATH via login shell)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PI_HOST="${PI_HOST:-pi5}"
DEPLOY_DIR="${PI_DEPLOY_DIR:-/home/pi/esp32_projects}"   # override via $PI_DEPLOY_DIR
HUB_SERVER_CMD="python3 hub.py --server http://localhost:5000"
TRIP_HUB_CMD="python3 trip_server.py"

ALL_DIRS=(Hub_Server Hub_Server_Firmware Trip_Hub)
SELECTED=()
DO_FLASH=1
DO_RESTART=1

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-flash)   DO_FLASH=0   ; shift ;;
    --no-restart) DO_RESTART=0 ; shift ;;
    --only)       SELECTED+=("$2") ; shift 2 ;;
    -h|--help)    sed -n '2,18p' "$0" ; exit 0 ;;
    *) echo "unknown arg: $1" >&2 ; exit 2 ;;
  esac
done

DIRS=("${SELECTED[@]:-${ALL_DIRS[@]}}")

echo "==> Syncing to ${PI_HOST}:${DEPLOY_DIR}"
for d in "${DIRS[@]}"; do
  if [[ ! -d "${SCRIPT_DIR}/${d}" ]]; then
    echo "  ! ${d}: missing locally, skipping"
    continue
  fi
  if [[ "$d" == "Trip_Hub" ]]; then
    # Trip_Hub holds the canonical trips.db / profiles.json / deleted_trips.json
    # ON THE PI. Pushing the local snapshot would overwrite live data, so
    # we sync only source files.
    echo "  - ${d}/  (source only — data files preserved on Pi)"
    ssh "${PI_HOST}" "mkdir -p ${DEPLOY_DIR}/Trip_Hub"
    shopt -s nullglob
    files=("${SCRIPT_DIR}/Trip_Hub"/*.py "${SCRIPT_DIR}/Trip_Hub"/*.html)
    shopt -u nullglob
    if (( ${#files[@]} > 0 )); then
      scp -q "${files[@]}" "${PI_HOST}:${DEPLOY_DIR}/Trip_Hub/"
    fi
  else
    echo "  - ${d}/"
    scp -r -q "${SCRIPT_DIR}/${d}" "${PI_HOST}:${DEPLOY_DIR}/"
  fi
done

# Whether Pico A flash was requested AND Hub_Server_Firmware is in the sync set.
HAS_FW=0
for d in "${DIRS[@]}"; do [[ "$d" == "Hub_Server_Firmware" ]] && HAS_FW=1; done

if [[ "${DO_RESTART}" == "1" ]] || { [[ "${DO_FLASH}" == "1" ]] && [[ "${HAS_FW}" == "1" ]]; }; then
  echo "==> Stopping services (Hub_Server frees /dev/ttyACM0; Trip_Hub frees :5000)"
  ssh "${PI_HOST}" "tmux kill-session -t hub_server 2>/dev/null || true ; tmux kill-session -t trip_hub 2>/dev/null || true ; pkill -f 'python3 .*[h]ub.py' || true ; pkill -f 'python3 .*[t]rip_server.py' || true ; sleep 1"
fi

if [[ "${DO_FLASH}" == "1" ]] && [[ "${HAS_FW}" == "1" ]]; then
  echo "==> Flashing Pico A via mpremote"
  ssh "${PI_HOST}" "bash -lc '
    set -e
    cd ${DEPLOY_DIR}/Hub_Server_Firmware
    files=(*.py)
    echo \"  copying \${#files[@]} .py files\"
    mpremote cp \"\${files[@]}\" :
    echo \"  reset\"
    mpremote reset
  '"
  # wait for /dev/ttyACM0 to come back after Pico A reboots
  ssh "${PI_HOST}" "until [ -e /dev/ttyACM0 ]; do sleep 0.3; done"
fi

if [[ "${DO_RESTART}" == "1" ]]; then
  echo "==> Starting Trip_Hub in tmux session 'trip_hub' (Flask :5000)"
  ssh "${PI_HOST}" "bash -lc '
    mkdir -p ~/trip_data
    cd ${DEPLOY_DIR}/Trip_Hub
    tmux new-session -d -s trip_hub \"${TRIP_HUB_CMD}\"
    sleep 1
    if tmux has-session -t trip_hub 2>/dev/null; then
      pid=\$(pgrep -f \"^python3 .*[t]rip_server\" | head -1 || true)
      echo \"  Trip_Hub PID: \${pid:-?}  (tmux session: trip_hub)\"
      echo \"  URL:    http://${PI_HOST}:5000/\"
    else
      echo \"  Trip_Hub FAILED to start\"
      exit 1
    fi
  '"

  echo "==> Starting Hub_Server in tmux session 'hub_server'"
  ssh "${PI_HOST}" "bash -lc '
    cd ${DEPLOY_DIR}/Hub_Server
    tmux new-session -d -s hub_server \"${HUB_SERVER_CMD}\"
    sleep 1
    if tmux has-session -t hub_server 2>/dev/null; then
      pid=\$(pgrep -f \"^python3 .*[l]ora_client\" | head -1 || true)
      echo \"  Hub_Server PID: \${pid:-?}  (tmux session: hub_server)\"
      echo \"  Attach:  ssh -t ${PI_HOST} tmux attach -t hub_server\"
      echo \"  Detach:  Ctrl-B then D\"
      echo \"  Tail:    ssh ${PI_HOST} tail -f ${DEPLOY_DIR}/Hub_Server/picoA_serial.log\"
    else
      echo \"  Hub_Server FAILED to start\"
      exit 1
    fi
  '"
fi

echo "==> Done."
