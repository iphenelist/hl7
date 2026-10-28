"""
OS-level service management for HL7 Relay.

Handles installing, uninstalling, and querying the background service on:
  - Linux:   systemd user service (~/.config/systemd/user/)
  - Windows: Task Scheduler (schtasks) with pythonw.exe
"""

import logging
import os
import platform
import subprocess
import sys

logger = logging.getLogger(__name__)

APP_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = platform.system() == "Windows"
SERVICE_NAME = "hl7-erpnext-sync"


# --------------------------------------------------------------------------- #
#  Public API
# --------------------------------------------------------------------------- #

def install_service() -> dict:
    """Install the relay service to run in the background and on boot.

    NOTE: This only *installs and enables* the service.  It does NOT start it
    immediately because the foreground Flask process is still holding the
    dashboard port.  Call ``start_background_service()`` after the
    foreground process exits.
    """
    if IS_WINDOWS:
        return _install_windows()
    else:
        return _install_linux()


def start_background_service() -> None:
    """Start the installed background service (after the foreground process exits).

    The start is delayed by a couple of seconds in a detached child process
    so the foreground process has released the dashboard port by the time
    the service actually starts.
    """
    if IS_WINDOWS:
        subprocess.Popen(
            ["cmd", "/c",
             f"timeout /t 3 /nobreak >nul & schtasks /run /tn {TASK_NAME}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        subprocess.Popen(
            ["sh", "-c",
             f"sleep 3; exec systemctl --user start {SERVICE_NAME}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def uninstall_service() -> dict:
    """Remove the background service and disable auto-start."""
    if IS_WINDOWS:
        return _uninstall_windows()
    else:
        return _uninstall_linux()


def is_service_installed() -> bool:
    """Check whether the background service is currently installed."""
    if IS_WINDOWS:
        return _is_installed_windows()
    else:
        return _is_installed_linux()


def get_service_status() -> dict:
    """Return the current service status."""
    return {
        "installed": is_service_installed(),
        "platform": "windows" if IS_WINDOWS else "linux",
    }


# --------------------------------------------------------------------------- #
#  Linux — systemd user service
# --------------------------------------------------------------------------- #

def _get_systemd_dir() -> str:
    """Return the systemd user service directory."""
    return os.path.expanduser("~/.config/systemd/user")


def _get_unit_path() -> str:
    """Return the full path to the service unit file."""
    return os.path.join(_get_systemd_dir(), f"{SERVICE_NAME}.service")


def _get_venv_python() -> str:
    """Return the path to the venv's Python interpreter."""
    venv_python = os.path.join(APP_DIR, "venv", "bin", "python")
    if os.path.isfile(venv_python):
        return venv_python
    return sys.executable


def _install_linux() -> dict:
    """Create and enable a systemd user service."""
    venv_python = _get_venv_python()
    server_py = os.path.join(APP_DIR, "server.py")
    log_file = os.path.join(APP_DIR, "hl7_relay.log")
    port = os.environ.get("HL7_DASHBOARD_PORT", "5151")

    # Paths are double-quoted in ExecStart because the app directory may
    # contain spaces; systemd splits unquoted arguments on whitespace.
    unit_content = f"""[Unit]
Description=ERPNext HL7 Relay Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={APP_DIR}
ExecStart="{venv_python}" "{server_py}"
Restart=on-failure
RestartSec=10
Environment=HL7_SERVICE_MODE=background
Environment=HL7_DASHBOARD_PORT={port}

StandardOutput=append:{log_file}
StandardError=append:{log_file}

[Install]
WantedBy=default.target
"""

    try:
        # Create the systemd user directory
        systemd_dir = _get_systemd_dir()
        os.makedirs(systemd_dir, exist_ok=True)

        # Write the unit file
        unit_path = _get_unit_path()
        with open(unit_path, "w", encoding="utf-8") as f:
            f.write(unit_content)

        # Reload systemd and enable (but do NOT start — port is still busy)
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, timeout=15,
        )
        subprocess.run(
            ["systemctl", "--user", "enable", SERVICE_NAME],
            capture_output=True, timeout=15,
        )

        # Enable lingering so the service runs even when user is not logged in
        try:
            user = os.environ.get("USER", os.getlogin())
            subprocess.run(
                ["loginctl", "enable-linger", user],
                capture_output=True, timeout=15,
            )
        except Exception:
            logger.warning(
                "Could not enable linger — service may not run when logged out."
            )

        logger.info("Systemd user service installed and enabled.")
        return {
            "status": "ok",
            "message": "Background service installed. "
                       "It will auto-start on boot.",
            "unit_path": unit_path,
        }

    except Exception as exc:
        logger.exception("Failed to install systemd service.")
        raise RuntimeError(f"Failed to install service: {exc}") from exc


def _uninstall_linux() -> dict:
    """Disable, remove, and stop the systemd user service.

    The stop happens LAST (detached, after a short delay): when this code
    runs inside the background service itself, ``systemctl stop`` kills
    this very process — so all cleanup must already be done, and the delay
    lets the HTTP response reach the browser first.
    """
    try:
        subprocess.run(
            ["systemctl", "--user", "disable", SERVICE_NAME],
            capture_output=True, timeout=15,
        )

        unit_path = _get_unit_path()
        if os.path.isfile(unit_path):
            os.remove(unit_path)

        subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True, timeout=15,
        )

        # Stop any running instance (possibly ourselves) after a delay
        subprocess.Popen(
            ["sh", "-c",
             f"sleep 2; exec systemctl --user stop {SERVICE_NAME}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        logger.info("Systemd user service uninstalled.")
        return {
            "status": "ok",
            "message": "Background service stopped and removed from auto-start.",
        }

    except Exception as exc:
        logger.exception("Failed to uninstall systemd service.")
        raise RuntimeError(f"Failed to uninstall service: {exc}") from exc


def _is_installed_linux() -> bool:
    """Check if the systemd user service unit file exists."""
    return os.path.isfile(_get_unit_path())


# --------------------------------------------------------------------------- #
#  Windows — Task Scheduler
# --------------------------------------------------------------------------- #

TASK_NAME = "ERPNextHL7SyncService"


def _get_venv_pythonw() -> str:
    """Return pythonw.exe (no console window) from the venv, if available."""
    venv_pythonw = os.path.join(APP_DIR, "venv", "Scripts", "pythonw.exe")
    if os.path.isfile(venv_pythonw):
        return venv_pythonw
    venv_python = os.path.join(APP_DIR, "venv", "Scripts", "python.exe")
    if os.path.isfile(venv_python):
        return venv_python
    return sys.executable


def _install_windows() -> dict:
    """Create a Windows startup task using Task Scheduler."""
    pythonw = _get_venv_pythonw()
    server_py = os.path.join(APP_DIR, "server.py")

    try:
        # Remove existing task if present
        subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True, timeout=15,
        )

        # Create a new task that runs at logon
        result = subprocess.run(
            [
                "schtasks", "/create",
                "/tn", TASK_NAME,
                "/tr", f'"{pythonw}" "{server_py}"',
                "/sc", "onlogon",
                "/rl", "highest",
                "/f",
            ],
            capture_output=True, text=True, timeout=15,
        )

        if result.returncode != 0:
            raise RuntimeError(f"schtasks failed: {result.stderr}")

        logger.info("Windows scheduled task installed and enabled.")
        return {
            "status": "ok",
            "message": "Background service installed. "
                       "It will auto-start when you log in.",
        }

    except Exception as exc:
        logger.exception("Failed to install Windows scheduled task.")
        raise RuntimeError(f"Failed to install service: {exc}") from exc


def _uninstall_windows() -> dict:
    """Remove the Windows startup task.

    Only auto-start registration is removed here; the running process is
    not killed (when this code runs inside the background service itself,
    ``schtasks /end`` would kill this very process mid-request — the API
    layer schedules a delayed self-exit instead).
    """
    try:
        result = subprocess.run(
            ["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
            capture_output=True, text=True, timeout=15,
        )

        if result.returncode != 0 and "cannot find" not in (result.stderr or "").lower():
            raise RuntimeError(f"schtasks delete failed: {result.stderr}")

        logger.info("Windows scheduled task uninstalled.")
        return {
            "status": "ok",
            "message": "Background service stopped and removed from auto-start.",
        }

    except Exception as exc:
        logger.exception("Failed to uninstall Windows scheduled task.")
        raise RuntimeError(f"Failed to uninstall service: {exc}") from exc


def _is_installed_windows() -> bool:
    """Check if the Windows scheduled task exists."""
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False
