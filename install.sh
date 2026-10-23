#!/usr/bin/env bash
#
# ssmouse installer for Linux (udev rule + Python package).
#
# Usage:
#   ./install.sh                 # install udev rule (sudo) and the package
#   ./install.sh --no-udev       # package only
#   ./install.sh --venv          # force a project-local .venv install
#   ./install.sh --pipx          # force pipx (recommended: puts `ssmouse` on PATH)
#   ./install.sh --no-gui        # CLI only (skip PySide6 and the menu entry)
#   ./install.sh --uninstall     # remove the udev rule
#
# The udev rule needs root, hence sudo.  The Python package is installed into
# your user environment (pipx or .venv) and never needs root.

set -euo pipefail

usage() {
    cat <<'USAGE'
ssmouse installer for Linux (udev rule + Python package).

Usage:
  ./install.sh                 # install udev rule (sudo) and the package
  ./install.sh --no-udev       # package only
  ./install.sh --venv          # force a project-local .venv install
  ./install.sh --pipx          # force pipx (recommended: puts `ssmouse` on PATH)
  ./install.sh --no-gui        # CLI only (skip PySide6 and the menu entry)
  ./install.sh --uninstall     # remove the udev rule

The udev rule needs root, hence sudo.  The Python package is installed into
your user environment (pipx or .venv) and never needs root.
USAGE
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
RULE_SRC="${SCRIPT_DIR}/udev/99-steelseries.rules"
RULE_DST="/etc/udev/rules.d/99-steelseries.rules"

INSTALL_UDEV=1
INSTALL_GUI=1
UNINSTALL=0
MODE="auto"

for arg in "$@"; do
    case "${arg}" in
        --no-udev) INSTALL_UDEV=0 ;;
        --no-gui) INSTALL_GUI=0 ;;
        --uninstall) UNINSTALL=1 ;;
        --venv) MODE="venv" ;;
        --pipx) MODE="pipx" ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown option ${arg}" >&2
            exit 2
            ;;
    esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "error: this installer is for Linux; see README for Windows/macOS" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# udev rule
# ---------------------------------------------------------------------------
if [[ "${UNINSTALL}" -eq 1 ]]; then
    if [[ -e "${RULE_DST}" ]]; then
        echo "==> removing ${RULE_DST} (sudo)"
        sudo rm -f "${RULE_DST}"
        sudo udevadm control --reload-rules
        sudo udevadm trigger --subsystem-match=hidraw --subsystem-match=usb
    fi
    echo "Done. Unplug/replug the mouse if it was in use."
    exit 0
fi

if [[ "${INSTALL_UDEV}" -eq 1 ]]; then
    if [[ ! -f "${RULE_SRC}" ]]; then
        echo "error: missing ${RULE_SRC}" >&2
        exit 1
    fi
    echo "==> installing udev rule to ${RULE_DST} (sudo)"
    sudo install -m 0644 "${RULE_SRC}" "${RULE_DST}"
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=hidraw --subsystem-match=usb
    echo "    Current hidraw permissions:"
    for node in /dev/hidraw*; do
        [[ -e "${node}" ]] || continue
        if udevadm info --query=property --name="${node}" 2>/dev/null | grep -q '^ID_VENDOR_ID=1038$'; then
            ls -l "${node}"
        fi
    done
fi

# ---------------------------------------------------------------------------
# Python package
# ---------------------------------------------------------------------------
echo "==> installing the ssmouse Python package"
PKG="${SCRIPT_DIR}"
if [[ "${INSTALL_GUI}" -eq 1 ]]; then
    PKG="${SCRIPT_DIR}[gui]"
fi

# Application-menu entry for the GUI; $1 = absolute path of ssmouse-gui.
install_desktop_entry() {
    [[ "${INSTALL_GUI}" -eq 1 ]] || return 0
    local apps="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
    mkdir -p "${apps}"
    sed "s|^Exec=.*|Exec=$1|" "${SCRIPT_DIR}/data/ssmouse-gui.desktop" > "${apps}/ssmouse-gui.desktop"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "${apps}" || true
    echo "    added \"ssmouse\" to the application menu"
}
if [[ "${MODE}" == "auto" ]]; then
    if command -v pipx >/dev/null 2>&1; then
        MODE="pipx"
    else
        MODE="venv"
    fi
fi

if [[ "${MODE}" == "pipx" ]]; then
    if ! command -v pipx >/dev/null 2>&1; then
        echo "error: pipx not found (install it or rerun with --venv)" >&2
        exit 1
    fi
    pipx install --force "${PKG}"
    install_desktop_entry "${PIPX_BIN_DIR:-${HOME}/.local/bin}/ssmouse-gui"
    echo
    echo "Installed. Run: ssmouse info   (or ssmouse-gui for the tray app)"
    exit 0
fi

# venv mode
UV=""
if command -v uv >/dev/null 2>&1; then
    UV="uv"
fi

if [[ -n "${UV}" ]]; then
    echo "    using uv to create/update ${SCRIPT_DIR}/.venv"
    "${UV}" venv --allow-existing --python "$(command -v python3)" "${SCRIPT_DIR}/.venv" >/dev/null
    "${UV}" pip install --python "${SCRIPT_DIR}/.venv/bin/python" -e "${PKG}"
elif python3 -c "import ensurepip" >/dev/null 2>&1; then
    python3 -m venv "${SCRIPT_DIR}/.venv"
    "${SCRIPT_DIR}/.venv/bin/pip" install --upgrade pip >/dev/null
    "${SCRIPT_DIR}/.venv/bin/pip" install -e "${PKG}"
else
    echo "error: cannot create a virtualenv: ensurepip is missing." >&2
    echo "       Install python3-venv (Debian/Ubuntu: sudo apt install python3-venv)" >&2
    echo "       or install uv/pipx and rerun this script." >&2
    exit 1
fi

install_desktop_entry "${SCRIPT_DIR}/.venv/bin/ssmouse-gui"
echo
echo "Installed into ${SCRIPT_DIR}/.venv"
echo "Run:"
echo "  ${SCRIPT_DIR}/.venv/bin/python -m ssmouse info"
echo "  ${SCRIPT_DIR}/.venv/bin/python -m ssmouse --json battery"
if [[ "${INSTALL_GUI}" -eq 1 ]]; then
    echo "  ${SCRIPT_DIR}/.venv/bin/ssmouse-gui"
fi
echo "or activate the venv: source ${SCRIPT_DIR}/.venv/bin/activate"
