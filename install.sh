#!/usr/bin/env bash
# Install Hermlet on a Raspberry Pi or any Debian-like Linux machine.
#
#   git clone https://github.com/j4ckxyz/hermlet.git && cd hermlet && bash install.sh
#
#   bash install.sh [--no-app] [--setup|--no-setup] [--autostart] [--yes]
#   bash install.sh --uninstall [--purge]
#
# Installs, for the current user only (no root except for apt packages):
#   ~/.local/share/hermlet/venv    the Python SDK and the `hermlet` command
#   ~/.local/bin/hermlet           a link to it
#   ~/.local/bin/hermlet-app       the native app, built here from app/
# Setup then happens in the app the first time it is opened: it starts everything it needs and
# asks for your Hermes address and sign-in. Without the app (or with --setup) the same questions
# are asked here, in the terminal, by `hermlet setup`.

set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${HERMLET_PREFIX:-$HOME/.local/share/hermlet}"
BIN="$HOME/.local/bin"
VENV="$PREFIX/venv"
BUILD="${XDG_CACHE_HOME:-$HOME/.cache}/hermlet/app-build"
DESKTOP="$HOME/.local/share/applications/hermlet.desktop"
AUTOSTART="$HOME/.config/autostart/hermlet.desktop"

WITH_APP=1
WITH_SETUP=auto   # auto: in the terminal only when there is no app to do it in
WITH_AUTOSTART=0
ASSUME_YES=0
UNINSTALL=0
PURGE=0

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: install.sh [options]

  --no-app      Install only the SDK and the hermlet command, not the native app.
  --setup       Connect to your Hermes here in the terminal, rather than in the app.
  --no-setup    Never ask in the terminal, even when the app was not installed.
  --autostart   Open the app when you log in to the desktop.
  --yes         Do not ask before installing system packages.
  --uninstall   Remove Hermlet. Your settings in ~/.config/hermlet stay unless
                --purge is also given.
  -h, --help    Show this help.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-app) WITH_APP=0 ;;
        --setup) WITH_SETUP=1 ;;
        --no-setup) WITH_SETUP=0 ;;
        --autostart) WITH_AUTOSTART=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --purge) PURGE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

ask() {
    local answer
    if [ "$ASSUME_YES" = 1 ] || [ ! -r /dev/tty ]; then return 0; fi
    printf '%s [Y/n] ' "$1" >/dev/tty
    read -r answer </dev/tty || true
    case "$answer" in ""|y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

uninstall() {
    say "Removing Hermlet"
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user disable --now hermlet.service 2>/dev/null || true
        rm -f "$HOME/.config/systemd/user/hermlet.service"
        systemctl --user daemon-reload 2>/dev/null || true
    fi
    rm -rf "$PREFIX" "$BUILD"
    rm -f "$BIN/hermlet" "$BIN/hermlet-app" "$DESKTOP" "$AUTOSTART"
    if [ "$PURGE" = 1 ]; then
        rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/hermlet"
        say "Removed, including this device's settings and sign-in."
    else
        say "Removed. Settings are kept in ${XDG_CONFIG_HOME:-$HOME/.config}/hermlet (add --purge to delete them)."
    fi
}

# Install the apt packages that are missing, after asking once.
need_packages() {
    local missing=() pkg
    command -v dpkg-query >/dev/null 2>&1 || return 0
    for pkg in "$@"; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" || missing+=("$pkg")
    done
    [ "${#missing[@]}" -eq 0 ] && return 0
    command -v apt-get >/dev/null 2>&1 || { warn "please install: ${missing[*]}"; return 1; }
    ask "Install system packages (${missing[*]})?" || return 1
    as_root apt-get update -qq
    as_root apt-get install -y --no-install-recommends "${missing[@]}"
}

install_sdk() {
    command -v python3 >/dev/null 2>&1 || need_packages python3 || die "Python 3 is needed."
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' ||
        die "Python 3.9 or later is needed (this is $(python3 --version 2>&1))."
    python3 -c 'import venv, ensurepip' 2>/dev/null || need_packages python3-venv ||
        die "The python3-venv package is needed."
    say "Installing the SDK into $VENV"
    mkdir -p "$PREFIX" "$BIN"
    [ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
    "$VENV/bin/python" -m pip install --quiet --upgrade pip
    "$VENV/bin/python" -m pip install --quiet --upgrade "$SOURCE"
    ln -sf "$VENV/bin/hermlet" "$BIN/hermlet"
    # Talking to Hermes needs a recorder and a player; typing works without them.
    command -v arecord >/dev/null 2>&1 || need_packages alsa-utils ||
        warn "no microphone tools; voice will be off until alsa-utils is installed"
}

install_app() {
    [ -f "$SOURCE/app/CMakeLists.txt" ] || { warn "app/ is not in this checkout; skipping the native app"; return 0; }
    need_packages cmake gcc g++ make git libsdl2-dev ||
        { warn "skipping the native app (build tools missing); 'hermlet chat' still works"; return 0; }
    # A Raspberry Pi with little memory builds more reliably with fewer jobs.
    local jobs mem_kb
    jobs="$(nproc 2>/dev/null || echo 2)"
    mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 4000000)"
    if [ "$mem_kb" -lt 900000 ]; then jobs=1; elif [ "$mem_kb" -lt 1900000 ] && [ "$jobs" -gt 2 ]; then jobs=2; fi
    say "Building the app (a few minutes on a small Raspberry Pi; $jobs job(s))"
    cmake -S "$SOURCE/app" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release >/dev/null
    cmake --build "$BUILD" -j "$jobs"
    install -m 755 "$BUILD/hermlet-app" "$BIN/hermlet-app"
    mkdir -p "$(dirname "$DESKTOP")"
    cat >"$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=Hermlet
Comment=Talk to your Hermes agent
Exec=$BIN/hermlet-app
Terminal=false
Categories=Utility;
EOF
    if [ "$WITH_AUTOSTART" = 1 ]; then
        mkdir -p "$(dirname "$AUTOSTART")"
        cp "$DESKTOP" "$AUTOSTART"
    fi
}

main() {
    [ "$(uname -s)" = Linux ] || die "This installer is for Linux. On other systems: pip install . && cmake -S app -B app/build"
    if [ "$UNINSTALL" = 1 ]; then uninstall; exit 0; fi
    [ "$(id -u)" -ne 0 ] || die "Run this as the person who will use Hermlet, not as root."
    install_sdk
    if [ "$WITH_APP" = 1 ]; then install_app; fi
    case ":$PATH:" in
        *":$BIN:"*) ;;
        *) warn "$BIN is not on your PATH yet; log out and in again, or run: export PATH=\"$BIN:\$PATH\"" ;;
    esac
    say "Installed."
    local have_app=0
    if [ -x "$BIN/hermlet-app" ]; then have_app=1; fi
    if [ "$WITH_SETUP" = auto ]; then
        if [ "$have_app" = 1 ]; then WITH_SETUP=0; else WITH_SETUP=1; fi
    fi
    if [ "$WITH_SETUP" = 1 ] && [ -r /dev/tty ]; then
        "$BIN/hermlet" setup </dev/tty
    elif [ "$have_app" = 0 ]; then
        echo "Next: hermlet setup"
    fi
    if [ "$have_app" = 1 ]; then
        echo "Open Hermlet from your menu, or run: hermlet-app"
        echo "The first time, it asks where your Hermes is and signs you in. Nothing else needs starting."
    fi
}

main
