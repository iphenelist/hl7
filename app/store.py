"""
JSON-file persistence for HL7 Relay.

All app state lives in a single ``config.json`` next to the application:
  - settings: ERPNext credentials, logs directory/retention
  - devices:  the list of lab machines. A "listen" device sends to its own
              MLLP port on this computer; a "connect" device is itself the
              server, and the app connects out to its host and port.

Message history is kept in memory only (lost on restart); the permanent
record is the rotating ``logs/error.log`` file for failures.

On first run, existing data is migrated from the legacy ``local_config.py``
(the old hardcoded single-port script config) if one is present — the old
config only ever had one port, so migration creates a single "Default"
device from it.
"""
import json
import logging
import os
import threading
from collections import deque
from datetime import datetime

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LEGACY_CONFIG = os.path.join(BASE_DIR, "local_config.py")

DEFAULT_SETTINGS = {
    "erpnext_url": "",
    "api_key": "",
    "api_secret": "",
    "logs_directory": "logs",
    "log_retention": 500,
}

_lock = threading.RLock()
_data: dict = {}          # {"settings": {...}, "devices": [...], "next_device_id": int}
_logs: deque = deque(maxlen=500)   # newest first; in-memory only
_next_log_id = 1


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _parse_dt(value):
    """ISO string -> datetime (or None)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
#  Compat objects
# --------------------------------------------------------------------------- #

class Settings:
    def __init__(self, raw: dict):
        self.erpnext_url = raw.get("erpnext_url", "")
        self.api_key = raw.get("api_key", "")
        self.api_secret = raw.get("api_secret", "")
        self.logs_directory = raw.get("logs_directory", "logs")
        self.log_retention = int(raw.get("log_retention") or 500)


class Device:
    def __init__(self, raw: dict):
        self.id = raw["id"]
        self.name = raw.get("name", "")
        self.port = int(raw.get("port") or 5600)
        self.mode = raw.get("mode") or "listen"   # "listen" | "connect"
        self.host = raw.get("host") or ""
        self.is_active = bool(raw.get("is_active", True))
        self.last_message_at = _parse_dt(raw.get("last_message_at"))
        self.created_at = _parse_dt(raw.get("created_at"))

    @property
    def last_message_status(self):
        for log in list(_logs):
            if log.device_id == self.id:
                return log.status
        return "Never"

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "port": self.port,
            "mode": self.mode,
            "host": self.host,
            "is_active": self.is_active,
            "last_message_at": self.last_message_at.isoformat() if self.last_message_at else None,
            "last_message_status": self.last_message_status,
        }


class MessageLog:
    def __init__(self, log_id, device_id, device_name, status,
                 machine_make=None, machine_model=None, lab_test_name=None,
                 erpnext_name=None, error=None):
        self.id = log_id
        self.device_id = device_id
        self.device_name = device_name
        self.status = status
        self.machine_make = machine_make
        self.machine_model = machine_model
        self.lab_test_name = lab_test_name
        self.erpnext_name = erpnext_name
        self.error = error
        self.timestamp = datetime.utcnow()

    def to_dict(self):
        return {
            "id": self.id,
            "device_id": self.device_id,
            "device_name": self.device_name,
            "status": self.status,
            "machine_make": self.machine_make,
            "machine_model": self.machine_model,
            "lab_test_name": self.lab_test_name,
            "erpnext_name": self.erpnext_name,
            "error": self.error,
            "timestamp": self.timestamp.isoformat(),
        }


# --------------------------------------------------------------------------- #
#  Load / save
# --------------------------------------------------------------------------- #

def load() -> None:
    """Load config.json into memory, migrating from local_config.py on first run."""
    global _data
    with _lock:
        if not os.path.exists(CONFIG_FILE) and os.path.exists(LEGACY_CONFIG):
            _migrate_from_local_config()

        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    _data = json.load(f)
            except (json.JSONDecodeError, IOError) as exc:
                logger.error("Failed to read %s: %s — starting with defaults", CONFIG_FILE, exc)
                _data = {}
        else:
            _data = {}

        _data.setdefault("settings", {})
        _data["settings"] = {**DEFAULT_SETTINGS, **_data["settings"]}
        _data.setdefault("devices", [])
        _data.setdefault("next_device_id", _max_device_id() + 1)


def _save() -> None:
    """Atomically write the in-memory state to config.json (caller holds lock)."""
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_FILE)


def _ensure_loaded() -> None:
    if not _data:
        load()


def _max_device_id() -> int:
    return max((d.get("id", 0) for d in _data.get("devices", [])), default=0)


# --------------------------------------------------------------------------- #
#  Settings
# --------------------------------------------------------------------------- #

def get_settings() -> Settings:
    with _lock:
        _ensure_loaded()
        return Settings(_data["settings"])


def save_settings(**fields) -> Settings:
    with _lock:
        _ensure_loaded()
        s = _data["settings"]
        for key, value in fields.items():
            if key not in DEFAULT_SETTINGS:
                continue
            s[key] = value
        s["erpnext_url"] = (s.get("erpnext_url") or "").rstrip("/")
        s["logs_directory"] = s.get("logs_directory") or "logs"
        try:
            s["log_retention"] = max(50, int(s.get("log_retention") or 500))
        except (ValueError, TypeError):
            s["log_retention"] = 500
        _save()
        return Settings(s)


# --------------------------------------------------------------------------- #
#  Devices
# --------------------------------------------------------------------------- #

def get_devices(active_only: bool = False) -> list:
    """All devices, newest first."""
    with _lock:
        _ensure_loaded()
        devices = [Device(d) for d in _data["devices"]]
        if active_only:
            devices = [d for d in devices if d.is_active]
        return list(reversed(devices))


def get_device(device_id: int):
    with _lock:
        _ensure_loaded()
        for d in _data["devices"]:
            if d["id"] == device_id:
                return Device(d)
        return None


def _port_in_use(port: int, exclude_id: int = None) -> bool:
    """Only listen devices bind a port here; a connect device's port is on the analyzer."""
    return any(
        d["port"] == port and d["id"] != exclude_id and d.get("mode", "listen") == "listen"
        for d in _data["devices"]
    )


def add_device(name, port, is_active=True, mode="listen", host="") -> Device:
    with _lock:
        _ensure_loaded()
        port = int(port)
        if mode == "listen" and _port_in_use(port):
            raise ValueError(f"Port {port} is already used by another device.")
        new_id = _data["next_device_id"]
        _data["next_device_id"] = new_id + 1
        raw = {
            "id": new_id,
            "name": name,
            "port": port,
            "mode": mode,
            "host": host,
            "is_active": bool(is_active),
            "last_message_at": None,
            "created_at": datetime.utcnow().isoformat(),
        }
        _data["devices"].append(raw)
        _save()
        return Device(raw)


def update_device(record_id: int, **fields):
    with _lock:
        _ensure_loaded()
        for d in _data["devices"]:
            if d["id"] == record_id:
                if "port" in fields:
                    fields["port"] = int(fields["port"])
                    mode = fields.get("mode", d.get("mode", "listen"))
                    if mode == "listen" and _port_in_use(fields["port"], exclude_id=record_id):
                        raise ValueError(f"Port {fields['port']} is already used by another device.")
                for key in ("name", "port", "mode", "host", "is_active"):
                    if key in fields:
                        d[key] = fields[key]
                _save()
                return Device(d)
        return None


def delete_device(device_id: int) -> bool:
    with _lock:
        _ensure_loaded()
        before = len(_data["devices"])
        _data["devices"] = [d for d in _data["devices"] if d["id"] != device_id]
        if len(_data["devices"]) < before:
            _save()
            return True
        return False


def touch_device(device_id: int) -> None:
    """Set last_message_at = now on a device."""
    with _lock:
        _ensure_loaded()
        for d in _data["devices"]:
            if d["id"] == device_id:
                d["last_message_at"] = datetime.utcnow().isoformat()
                _save()
                return


# --------------------------------------------------------------------------- #
#  Message logs (in-memory)
# --------------------------------------------------------------------------- #

def add_log(device_id, device_name, status, machine_make=None, machine_model=None,
            lab_test_name=None, erpnext_name=None, error=None) -> MessageLog:
    global _next_log_id
    with _lock:
        entry = MessageLog(
            _next_log_id, device_id, device_name, status,
            machine_make=machine_make, machine_model=machine_model,
            lab_test_name=lab_test_name, erpnext_name=erpnext_name, error=error,
        )
        _next_log_id += 1
        _logs.appendleft(entry)
        return entry


def get_logs(device_id=None, limit=50) -> list:
    """Recent message log entries, newest first."""
    with _lock:
        entries = [l for l in _logs if device_id is None or l.device_id == device_id]
        return entries[:limit]


def log_counts() -> dict:
    with _lock:
        return {
            "total": len(_logs),
            "failed": sum(1 for l in _logs if l.status == "Failed"),
            "success": sum(1 for l in _logs if l.status == "Success"),
        }


def latest_log_id() -> int:
    """ID of the newest in-memory message log, or 0 when there are none."""
    with _lock:
        return _next_log_id - 1


# --------------------------------------------------------------------------- #
#  One-time migration from the legacy local_config.py
# --------------------------------------------------------------------------- #

def _migrate_from_local_config() -> None:
    """Import settings + a single default device from the legacy local_config.py."""
    global _data
    import importlib.util

    logger.info("Migrating legacy local_config.py to config.json ...")
    try:
        spec = importlib.util.spec_from_file_location("legacy_local_config", LEGACY_CONFIG)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        settings = dict(DEFAULT_SETTINGS)
        settings["erpnext_url"] = (getattr(mod, "ERPNEXT_URL", "") or "").rstrip("/")
        settings["api_key"] = getattr(mod, "ERPNEXT_API_KEY", "") or ""
        settings["api_secret"] = getattr(mod, "ERPNEXT_API_SECRET", "") or ""
        settings["logs_directory"] = getattr(mod, "LOGS_DIRECTORY", "logs") or "logs"

        devices = [{
            "id": 1,
            "name": "Default",
            "port": 5600,
            "is_active": True,
            "last_message_at": None,
            "created_at": datetime.utcnow().isoformat(),
        }]

        _data = {
            "settings": settings,
            "devices": devices,
            "next_device_id": 2,
        }
        _save()
        os.replace(LEGACY_CONFIG, LEGACY_CONFIG + ".bak")
        logger.info(
            "Migration complete: 1 default device (port 5600) imported. "
            "Old config kept as %s.bak", LEGACY_CONFIG,
        )
    except Exception:
        logger.exception("local_config.py migration failed — starting with empty config.")
        _data = {}
