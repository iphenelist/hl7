"""
JSON actions behind the interface's buttons (``app/static/app.js``).

Pages themselves are rendered by ``pages.py``; these endpoints only change
things and report back: ``{"data": ...}`` on success and
``{"errors": [{"message": ...}]}`` on failure.
"""
from flask import Blueprint, jsonify, request

from app import store
from app.services.hl7_listener import get_listener_status, reload_device, stop_device

ui_api_bp = Blueprint("ui_api", __name__)


def _ok(data):
    return jsonify({"data": data})


def _error(message, status=400, title="Error"):
    return jsonify({"errors": [{"type": title, "title": title, "message": message}]}), status


def device_fields(data) -> dict:
    """Validate submitted device fields (form or JSON) into store fields.

    Raises ValueError with a message fit to show the user.
    """
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("Device name is required.")
    raw_port = data.get("port")
    try:
        port = int(raw_port) if str(raw_port or "").strip() else 5600
    except (TypeError, ValueError):
        raise ValueError("Port must be a whole number.")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    mode = data.get("mode") or "listen"
    if mode not in ("listen", "connect"):
        raise ValueError("Connection mode must be 'listen' or 'connect'.")
    host = (data.get("host") or "").strip()
    if mode == "connect" and not host:
        raise ValueError("The machine's IP address is required when the app connects to it.")
    return {
        "name": name, "port": port, "mode": mode,
        "host": host if mode == "connect" else "",
        "is_active": bool(data.get("is_active")),
    }


def _get_device(device_id):
    device = store.get_device(device_id)
    if device is None:
        return None, _error(f"Device {device_id} does not exist.", 404, "Not found")
    return device, None


# --------------------------------------------------------------------------- #
#  Live state
# --------------------------------------------------------------------------- #

@ui_api_bp.route("/state")
def state():
    """Polled by every page: a new log id means a new message arrived."""
    status = get_listener_status()
    return _ok({
        "last_log_id": store.latest_log_id(),
        "listening": sum(1 for s in status.values() if s["running"]),
        "stopped": sum(1 for d in store.get_devices(active_only=True)
                       if not status.get(d.id, {}).get("running")),
    })


# --------------------------------------------------------------------------- #
#  Devices
# --------------------------------------------------------------------------- #

@ui_api_bp.route("/devices", methods=["POST"])
def create_device():
    try:
        device = store.add_device(**device_fields(request.get_json(silent=True) or {}))
    except ValueError as exc:
        return _error(str(exc))
    reload_device(device.id)
    return _ok(device.to_dict())


@ui_api_bp.route("/devices/<int:device_id>", methods=["PUT"])
def update_device(device_id):
    device, err = _get_device(device_id)
    if err:
        return err
    try:
        device = store.update_device(device_id, **device_fields(request.get_json(silent=True) or {}))
    except ValueError as exc:
        return _error(str(exc))
    reload_device(device.id)
    return _ok(device.to_dict())


@ui_api_bp.route("/devices/<int:device_id>/toggle", methods=["POST"])
def toggle_device(device_id):
    device, err = _get_device(device_id)
    if err:
        return err
    device = store.update_device(device_id, is_active=not device.is_active)
    if device.is_active:
        reload_device(device_id)
    else:
        stop_device(device_id)
    return _ok(device.to_dict())


@ui_api_bp.route("/devices/<int:device_id>", methods=["DELETE"])
def delete_device(device_id):
    device, err = _get_device(device_id)
    if err:
        return err
    stop_device(device_id)
    store.delete_device(device_id)
    return _ok({"deleted": device.name})


# --------------------------------------------------------------------------- #
#  Settings
# --------------------------------------------------------------------------- #

def _settings_dict(settings):
    return {
        "erpnext_url": settings.erpnext_url,
        "api_key": settings.api_key,
        "api_secret": settings.api_secret,
        "logs_directory": settings.logs_directory,
    }


@ui_api_bp.route("/settings", methods=["PUT"])
def save_settings():
    """Save the fields that were sent; others keep their current value."""
    data = request.get_json(silent=True) or {}
    fields = {
        key: (data[key] or "").strip()
        for key in ("erpnext_url", "api_key", "api_secret", "logs_directory")
        if key in data
    }
    settings = store.save_settings(**fields)
    return _ok({"settings": _settings_dict(settings)})


@ui_api_bp.route("/settings/test-erpnext", methods=["POST"])
def test_erpnext():
    from app.services.erpnext_service import ERPNextClient

    settings = store.get_settings()
    client = ERPNextClient(settings.erpnext_url, settings.api_key, settings.api_secret)
    result = client.test_connection()
    if result["success"]:
        return _ok({"success": True, "message": f"Connected successfully as: {result['user']}"})
    return _ok({"success": False, "message": f"Connection failed: {result['error']}"})
