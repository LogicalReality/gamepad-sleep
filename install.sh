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

trap 'notify "Installation failed" "Check logs or run ./install.sh from Konsole for details."' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
SYSTEMD_DIR="${HOME}/.config/systemd/user"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/gamepad-sleep"

echo "==> Preparing directories..."
mkdir -p "${BIN_DIR}" "${SYSTEMD_DIR}" "${CONFIG_DIR}"
chmod +x "${SCRIPT_DIR}/gamepad-sleep.py" "${SCRIPT_DIR}/install.sh" "${SCRIPT_DIR}/uninstall.sh" "${SCRIPT_DIR}/update.sh" 2>/dev/null || true

echo "==> Installing script to ${BIN_DIR}/gamepad-sleep.py"
install -m 755 "${SCRIPT_DIR}/gamepad-sleep.py" "${BIN_DIR}/gamepad-sleep.py"

echo "==> Installing systemd user unit to ${SYSTEMD_DIR}/gamepad-sleep.service"
install -m 644 "${SCRIPT_DIR}/gamepad-sleep.service" "${SYSTEMD_DIR}/gamepad-sleep.service"

if [[ ! -f "${CONFIG_DIR}/config.toml" ]]; then
    install -m 644 "${SCRIPT_DIR}/config.toml" "${CONFIG_DIR}/config.toml"
    echo "==> Created default config at ${CONFIG_DIR}/config.toml"
else
    echo "==> Keeping existing config at ${CONFIG_DIR}/config.toml"
fi

if git -C "${SCRIPT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "${SCRIPT_DIR}" rev-parse HEAD > "${CONFIG_DIR}/.version"
elif [[ -f "${SCRIPT_DIR}/.version" ]]; then
    install -m 644 "${SCRIPT_DIR}/.version" "${CONFIG_DIR}/.version"
fi

echo "==> Reloading systemd user daemon..."
systemctl --user daemon-reload

echo "==> Enabling and starting gamepad-sleep.service..."
systemctl --user enable gamepad-sleep.service
systemctl --user restart gamepad-sleep.service

echo "==> Done! gamepad-sleep.service is active."
notify "gamepad-sleep installed" "Service is running. Config: ~/.config/gamepad-sleep/config.toml"

parent_proc="$(ps -o comm= -p "${PPID}" 2>/dev/null || true)"
if [[ -t 1 && ( -n "${GAMEPAD_SLEEP_IN_TERM:-}" || ! "${parent_proc}" =~ ^(bash|zsh|fish|sh|dash)$ ) ]]; then
    echo
    read -r -t 10 -p "Press Enter to close (auto-closing in 10s)... " || true
fi
