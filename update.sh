#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-${HOME}/.config}/gamepad-sleep"
DEFAULT_REPO="LogicalReality/gamepad-sleep"
BRANCH="main"

cd "${SCRIPT_DIR}"

CHECK_ONLY=false
if [[ "${1:-}" == "--check" ]]; then
    CHECK_ONLY=true
fi

if [[ "${CHECK_ONLY}" == false && ! -t 1 && -z "${GAMEPAD_SLEEP_IN_TERM:-}" ]]; then
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

github_compare_url() {
    local repo="$1"
    local local_sha="$2"
    local remote_sha="$3"
    if [[ "${local_sha}" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
        echo "https://github.com/${repo}/compare/${local_sha:0:7}...${remote_sha:0:7}"
    else
        echo "https://github.com/${repo}/commits/${BRANCH}"
    fi
}

repo_from_git() {
    local remote_url
    remote_url="$(git remote get-url origin 2>/dev/null || true)"
    remote_url="${remote_url%.git}"
    if [[ "${remote_url}" =~ ^git@github\.com:(.+)$ ]]; then
        echo "${BASH_REMATCH[1]}"
    elif [[ "${remote_url}" =~ ^https://github\.com/(.+)$ ]]; then
        echo "${BASH_REMATCH[1]}"
    else
        echo "${DEFAULT_REPO}"
    fi
}

apply_tarball_update() {
    local repo="$1"
    local remote_sha="$2"
    local tmp_dir
    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "${tmp_dir}"' RETURN

    echo "==> Downloading latest archive from ${repo} (${BRANCH})..."
    curl -fsSL "https://github.com/${repo}/archive/refs/heads/${BRANCH}.tar.gz" -o "${tmp_dir}/src.tar.gz"
    echo "==> Extracting update..."
    tar -xzf "${tmp_dir}/src.tar.gz" -C "${tmp_dir}"
    local extracted
    extracted="$(find "${tmp_dir}" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
    cp -a "${extracted}/." "${SCRIPT_DIR}/"
    chmod +x "${SCRIPT_DIR}/gamepad-sleep.py" "${SCRIPT_DIR}/install.sh" "${SCRIPT_DIR}/uninstall.sh" "${SCRIPT_DIR}/update.sh"
    echo "${remote_sha}" > "${SCRIPT_DIR}/.version"
    echo "==> Re-running installer..."
    "${SCRIPT_DIR}/install.sh"
}

USE_GIT=false
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    USE_GIT=true
    REPO="$(repo_from_git)"
    echo "==> Checking for updates via git (${REPO})..."
    git fetch --quiet origin
    UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
    if [[ -z "${UPSTREAM}" ]]; then
        echo "No upstream branch configured."
        exit 1
    fi
    LOCAL_SHA="$(git rev-parse HEAD)"
    REMOTE_SHA="$(git rev-parse "${UPSTREAM}")"
else
    REPO="${DEFAULT_REPO}"
    echo "==> Checking for updates via GitHub API (${REPO})..."
    LOCAL_SHA=""
    if [[ -f "${CONFIG_DIR}/.version" ]]; then
        LOCAL_SHA="$(tr -d '[:space:]' < "${CONFIG_DIR}/.version")"
    elif [[ -f "${SCRIPT_DIR}/.version" ]]; then
        LOCAL_SHA="$(tr -d '[:space:]' < "${SCRIPT_DIR}/.version")"
    fi
    REMOTE_SHA="$(curl -fsSL "https://api.github.com/repos/${REPO}/commits/${BRANCH}" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"
fi

pause_on_gui_exit() {
    local parent_proc
    parent_proc="$(ps -o comm= -p "${PPID}" 2>/dev/null || true)"
    if [[ -t 1 && ( -n "${GAMEPAD_SLEEP_IN_TERM:-}" || ! "${parent_proc}" =~ ^(bash|zsh|fish|sh|dash)$ ) ]]; then
        echo
        read -r -t 10 -p "Press Enter to close (auto-closing in 10s)... " || true
    fi
}

if [[ -n "${LOCAL_SHA}" && "${LOCAL_SHA}" == "${REMOTE_SHA}" ]]; then
    echo "==> gamepad-sleep is already up to date (${LOCAL_SHA:0:7})."
    pause_on_gui_exit
    exit 0
fi

COMPARE_URL="$(github_compare_url "${REPO}" "${LOCAL_SHA}" "${REMOTE_SHA}")"

apply_update() {
    if [[ "${USE_GIT}" == true ]]; then
        echo "==> Pulling latest changes..."
        git pull --ff-only
        echo "==> Re-running installer..."
        "${SCRIPT_DIR}/install.sh"
    else
        apply_tarball_update "${REPO}" "${REMOTE_SHA}"
    fi
}

if [[ "${CHECK_ONLY}" == true ]]; then
    if command -v notify-send >/dev/null 2>&1; then
        ACTION="$(notify-send -a "gamepad-sleep" --wait \
            --action="update=Update now" \
            --action="github=View changes on GitHub" \
            "Update available for gamepad-sleep" \
            "New updates are available. Choose an action or run ./update.sh later." 2>/dev/null || true)"

        case "${ACTION}" in
            update)
                apply_update
                ;;
            github)
                if command -v xdg-open >/dev/null 2>&1; then
                    xdg-open "${COMPARE_URL}" >/dev/null 2>&1 &
                fi
                ;;
            *)
                if [[ -z "${ACTION}" ]]; then
                    notify "Update available for gamepad-sleep" "Run ./update.sh in ${SCRIPT_DIR} to update."
                fi
                ;;
        esac
    else
        echo "Update available: ${COMPARE_URL}"
    fi
    exit 0
fi

echo "Update available (${LOCAL_SHA:0:7} -> ${REMOTE_SHA:0:7})."
echo "Changes: ${COMPARE_URL}"
apply_update
pause_on_gui_exit
