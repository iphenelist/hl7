#!/usr/bin/env bash
# HL7 Relay — one-click setup for Linux.
#
# Installs everything the app needs (Python 3.9+ with venv/pip, and git),
# fetches the app if this script is run on its own, then launches it.
#
#   ./setup_linux.sh
#   curl -fsSL https://raw.githubusercontent.com/Aakvatech-Limited/hl7/version-15-hotfix/setup_linux.sh | bash
#
# Set HL7_RELAY_HOME to choose where a standalone install goes
# (default: ~/hl7-relay).
set -euo pipefail

REPO_URL="https://github.com/Aakvatech-Limited/hl7.git"
REPO_BRANCH="version-15-hotfix"
INSTALL_DIR="${HL7_RELAY_HOME:-$HOME/hl7-relay}"

step() { echo "[…] $*"; }
ok()   { echo "[✓] $*"; }
fail() { echo "[✗] $*" >&2; exit 1; }

echo "============================================"
echo "  HL7 Relay"
echo "============================================"
echo ""
echo "Starting automatic setup and launch..."
echo ""

# --------------------------------------------------------------------------- #
#  System packages
# --------------------------------------------------------------------------- #
SUDO=()
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo &>/dev/null && { [ -t 0 ] || [ -t 1 ] || sudo -n true 2>/dev/null; }; then
        SUDO=(sudo)
    elif command -v pkexec &>/dev/null; then
        # Launched from a file manager without a terminal: graphical password prompt
        SUDO=(pkexec)
    elif command -v sudo &>/dev/null; then
        SUDO=(sudo)
    fi
fi

install_packages() {
    # Usage: install_packages <what> — installs python + git for this distro
    if [ "$(id -u)" -ne 0 ] && [ ${#SUDO[@]} -eq 0 ]; then
        fail "Need administrator rights to install $1, but sudo is not available.
    Install Python 3.9+ (with venv) and git manually, then run this again."
    fi
    step "Installing $1 (you may be asked for your password)..."
    if command -v apt-get &>/dev/null; then
        "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get update -y
        "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y \
            python3 python3-venv python3-pip git ca-certificates
    elif command -v dnf &>/dev/null; then
        "${SUDO[@]}" dnf install -y python3 python3-pip git
    elif command -v yum &>/dev/null; then
        "${SUDO[@]}" yum install -y python3 python3-pip git
    elif command -v zypper &>/dev/null; then
        "${SUDO[@]}" zypper --non-interactive install python3 python3-pip git
    elif command -v pacman &>/dev/null; then
        "${SUDO[@]}" pacman -Sy --noconfirm --needed python python-pip git
    elif command -v apk &>/dev/null; then
        "${SUDO[@]}" apk add --no-cache python3 py3-pip git
    else
        fail "Unsupported Linux distribution (no apt, dnf, yum, zypper, pacman or apk).
    Install Python 3.9+ (with venv) and git manually, then run this again."
    fi
}

# Python must be 3.9+ and able to create a venv with pip (Debian/Ubuntu ship
# venv/ensurepip separately in python3-venv).
python_ok() {
    "$1" -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 9) else 1)' &>/dev/null
}

find_python() {
    local candidate
    for candidate in python3 python python3.13 python3.12 python3.11 python3.10 python3.9; do
        if command -v "$candidate" &>/dev/null && python_ok "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

NEED=()
PYTHON="$(find_python || true)"
[ -z "$PYTHON" ] && NEED+=("Python")
command -v git &>/dev/null || NEED+=("git")

if [ ${#NEED[@]} -gt 0 ]; then
    install_packages "${NEED[*]}"
    hash -r
    PYTHON="$(find_python || true)"
    [ -n "$PYTHON" ] || fail "Python 3.9+ with venv support is still not available.
    Your distribution's Python may be too old — install Python 3.9+ manually."
    command -v git &>/dev/null || fail "git is still not available after installing it."
fi
ok "Python: $("$PYTHON" --version 2>&1) ($PYTHON)"
ok "Git:    $(git --version)"

# --------------------------------------------------------------------------- #
#  App files
# --------------------------------------------------------------------------- #
SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/run.py" ]; then
    APP_DIR="$SCRIPT_DIR"
elif [ -f "$INSTALL_DIR/run.py" ]; then
    APP_DIR="$INSTALL_DIR"
    ok "Using existing install in $APP_DIR"
else
    if [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
        fail "$INSTALL_DIR exists but is not a HL7 Relay install.
    Remove it or set HL7_RELAY_HOME to another folder."
    fi
    step "Downloading HL7 Relay into $INSTALL_DIR..."
    git clone --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
    APP_DIR="$INSTALL_DIR"
    ok "Downloaded."
fi

# --------------------------------------------------------------------------- #
#  Launch (run.py creates the venv and installs pip dependencies)
# --------------------------------------------------------------------------- #
exec "$PYTHON" "$APP_DIR/run.py"
