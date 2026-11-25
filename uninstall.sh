#!/usr/bin/env bash
set -euo pipefail

if [[ ! -t 1 && -z "${GAMEPAD_SLEEP_IN_TERM:-}" ]]; then
    for term in konsole ptyxis x-terminal-emulator xterm; do
        if command -v "${term}" >/dev/null 2>&1; then
            exec env GAMEPAD_SLEEP_IN_TERM=1 "${term}" -e "$0" "$@"
        fi
    done
fi

notify() {
    local title="$1"
    local body="$2"
    if command -v notify-send >/dev/null 2>&1; then
        notify-send -a "gamepad-sleep" "${title}" "${body}" 2>/dev/null || true
    fi
}

BIN_PATH="${HOME}/.local/bin/gamepad-sleep.py"
SERVICE_PATH="${HOME}/.config/systemd/user/gamepad-sleep.service"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/gamepad-sleep"

echo "==> Stopping and disabling gamepad-sleep.service..."
systemctl --user disable --now gamepad-sleep.service 2>/dev/null || true

echo "==> Removing ${BIN_PATH} and ${SERVICE_PATH}..."
rm -f "${BIN_PATH}" "${SERVICE_PATH}"

echo "==> Reloading systemd user daemon..."
systemctl --user daemon-reload

echo "==> Done! Removed gamepad-sleep script and user service."

CONFIG_MSG="Service removed."
if [[ -d "${CONFIG_DIR}" ]]; then
    if [[ -t 0 ]]; then
        read -r -p "Remove config directory ${CONFIG_DIR}? [y/N]: " reply || true
        if [[ "${reply:-}" =~ ^[Yy]$ ]]; then
            rm -rf "${CONFIG_DIR}"
            echo "==> Removed config directory ${CONFIG_DIR}."
            CONFIG_MSG="Service and config removed."
        else
            echo "==> Config directory preserved at ${CONFIG_DIR}."
            CONFIG_MSG="Service removed. Config kept at ~/.config/gamepad-sleep"
        fi
    else
        echo "==> Config directory preserved at ${CONFIG_DIR}."
        CONFIG_MSG="Service removed. Config kept at ~/.config/gamepad-sleep"
    fi
fi

notify "gamepad-sleep uninstalled" "${CONFIG_MSG}"

parent_proc="$(ps -o comm= -p "${PPID}" 2>/dev/null || true)"
if [[ -t 1 && ( -n "${GAMEPAD_SLEEP_IN_TERM:-}" || ! "${parent_proc}" =~ ^(bash|zsh|fish|sh|dash)$ ) ]]; then
    echo
    read -r -t 10 -p "Press Enter to close (auto-closing in 10s)... " || true
fi
