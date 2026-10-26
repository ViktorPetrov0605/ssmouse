#!/usr/bin/env bash
#
# ssmouse installer for Linux: udev rule + binaries + application-menu entry.
#
# Works from a release tarball (binaries next to this script) or from a source
# checkout (builds them with cargo).
#
#   ./install.sh              install everything (the udev rule needs sudo)
#   ./install.sh --no-udev    skip the udev rule
#   ./install.sh --uninstall  remove binaries, menu entry, autostart and the udev rule

set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APPS_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
AUTOSTART="${XDG_CONFIG_HOME:-${HOME}/.config}/autostart/ssmouse-gui.desktop"
RULE_DST="/etc/udev/rules.d/99-steelseries.rules"
INSTALL_UDEV=1
UNINSTALL=0

for arg in "$@"; do
    case "${arg}" in
        --no-udev) INSTALL_UDEV=0 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) sed -n '3,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "error: unknown option ${arg}" >&2; exit 2 ;;
    esac
done

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "error: this installer is for Linux; see README for Windows/macOS" >&2
    exit 2
fi

reload_udev() {
    sudo udevadm control --reload-rules
    sudo udevadm trigger --subsystem-match=hidraw --subsystem-match=usb
}

if [[ "${UNINSTALL}" -eq 1 ]]; then
    rm -f "${BIN_DIR}/ssmouse" "${BIN_DIR}/ssmouse-gui" "${APPS_DIR}/ssmouse-gui.desktop" "${AUTOSTART}"
    if [[ -e "${RULE_DST}" ]]; then
        echo "==> removing ${RULE_DST} (sudo)"
        sudo rm -f "${RULE_DST}"
        reload_udev
    fi
    echo "ssmouse removed. Your settings stay in ~/.config/ssmouse."
    exit 0
fi

# -- binaries ----------------------------------------------------------------
if [[ -x "${HERE}/ssmouse" && -x "${HERE}/ssmouse-gui" ]]; then
    SRC="${HERE}"
elif [[ -f "${HERE}/Cargo.toml" ]]; then
    command -v cargo >/dev/null 2>&1 || { echo "error: cargo not found (https://rustup.rs) - or use a release download" >&2; exit 1; }
    echo "==> building (cargo build --release)"
    cargo build --release --manifest-path "${HERE}/Cargo.toml"
    SRC="${HERE}/target/release"
else
    echo "error: ssmouse binaries not found next to install.sh" >&2
    exit 1
fi

echo "==> installing ssmouse and ssmouse-gui to ${BIN_DIR}"
mkdir -p "${BIN_DIR}"
install -m 755 "${SRC}/ssmouse" "${SRC}/ssmouse-gui" "${BIN_DIR}/"

DESKTOP_SRC="${HERE}/ssmouse-gui.desktop"
[[ -f "${DESKTOP_SRC}" ]] || DESKTOP_SRC="${HERE}/data/ssmouse-gui.desktop"
mkdir -p "${APPS_DIR}"
sed "s|^Exec=.*|Exec=${BIN_DIR}/ssmouse-gui|" "${DESKTOP_SRC}" > "${APPS_DIR}/ssmouse-gui.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "${APPS_DIR}" || true
echo "    added \"ssmouse\" to the application menu"

# -- udev rule -----------------------------------------------------------------
if [[ "${INSTALL_UDEV}" -eq 1 ]]; then
    RULE_SRC="${HERE}/99-steelseries.rules"
    [[ -f "${RULE_SRC}" ]] || RULE_SRC="${HERE}/udev/99-steelseries.rules"
    echo "==> installing udev rule to ${RULE_DST} (sudo)"
    sudo install -m 0644 "${RULE_SRC}" "${RULE_DST}"
    reload_udev
    echo "    replug the receiver if ssmouse reports a permission error"
fi

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) echo "note: ${BIN_DIR} is not on your PATH" ;;
esac
echo
echo "Done. Start \"ssmouse\" from the application menu, or run: ssmouse info"
