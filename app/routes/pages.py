"""
The web interface: server-rendered pages styled like Frappe v16.

Each page is plain HTML from ``app/templates`` with one stylesheet and one
small script from ``app/static`` (no build step, no Node). Buttons that
change something call the JSON actions in ``ui_api.py``; ``app.js`` then
re-fetches the page to show the result, and does the same whenever a new
message arrives.

The wording shown for each device's state and each failed message is
decided here, so templates only lay it out.
"""
import re
import socket

from flask import Blueprint, abort, render_template

from app import store
from app.services.hl7_listener import get_listener_status

pages_bp = Blueprint("pages", __name__)


# --------------------------------------------------------------------------- #
#  View models
# --------------------------------------------------------------------------- #

def _utc(dt):
    """Naive UTC datetime (how the store keeps them) -> ISO string with Z."""
    return dt.isoformat() + "Z" if dt else None


def lan_ip():
    """This PC's LAN address (what a lab machine should send messages to)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))   # no packet is sent for UDP connect
            return s.getsockname()[0]
    except OSError:
        return None


def explain_error(message):
    """A plain explanation for the errors relays commonly fail with, or None.

    The raw exception text stays available behind "Details".
    """
    if not message:
        return None
    http = re.search(r"HTTPS?ConnectionPool\(host='([^']+)', port=(\d+)\)", message)
    if http:
        where = f"{http[1]}:{http[2]}"
        if re.search(r"refused|Errno 111", message, re.I):
            return (f"ERPNext didn't answer at {where}. Check that it is running "
                    "and that the ERPNext URL in Settings is right.")
        if re.search(r"timed out|timeout", message, re.I):
            return f"ERPNext at {where} took too long to answer."
        if re.search(r"Name or service not known|NameResolution|getaddrinfo|nodename", message, re.I):
            return f"The ERPNext address {http[1]} could not be found. Check the ERPNext URL in Settings."
        return f"Could not reach ERPNext at {where}."
    if re.search(r"ERPNext rejected the request \((401|403)\)", message):
        return ("ERPNext rejected the API key or secret. Check them in Settings, "
                "and that the user can create Lab Machine Message.")
    rejected = re.search(r"ERPNext rejected the request \(\d+\): (.+)", message)
    if rejected:
        return f"ERPNext refused the message: {rejected[1]}"
    if re.search(r"Invalid URL|No connection adapters|MissingSchema", message, re.I):
        return "The ERPNext URL in Settings is missing or incomplete."
    return None


def _explain_listen_error(error, port):
    if not error:
        return None
    if re.search(r"Errno 98|Errno 48|10048|address already in use", error, re.I):
        return (f"Port {port} is already used by another program on this computer. "
                "Pick another port, or close that program.")
    if re.search(r"Errno 13|permission denied|10013", error, re.I):
        return f"This computer does not allow the app to use port {port}. Use a port above 1024."
    return error


def _explain_connect_error(error, at, port):
    if not error:
        return None
    if re.search(r"Errno 111|10061|connection refused", error, re.I):
        return f"{at} refused the connection. Check the machine is on and its LIS server port is {port}. Retrying."
    if re.search(r"Errno 113|Errno 101|10065|10051|no route|unreachable|No answer|timed out", error, re.I):
        return f"No answer from {at}. Check the IP address and the network cable. Retrying."
    if re.search(r"closed the connection|Connection lost", error, re.I):
        return f"{error} Reconnecting."
    return f"{error} Retrying."


def _state(device, online, error):
    """Badge label, theme and an optional note for the device's connection."""
    connect = device.mode == "connect"
    if not device.is_active:
        note = "Not connecting to the machine." if connect else "The port is closed."
        return {"label": "Inactive", "theme": "gray", "note": note}
    if online:
        return {"label": "Connected" if connect else "Listening", "theme": "green", "note": None}
    if connect:
        at = f"{device.host}:{device.port}"
        return {"label": "Connecting", "theme": "amber",
                "note": _explain_connect_error(error, at, device.port) or "Trying to reach the machine."}
    return {"label": "Stopped", "theme": "red",
            "note": _explain_listen_error(error, device.port) or "The listener is not running."}


def _log(log):
    return {
        **log.to_dict(),
        "timestamp": _utc(log.timestamp),
        "machine": " ".join(filter(None, [log.machine_make, log.machine_model])) or "—",
        "explained": explain_error(log.error),
    }


def _device(device, status):
    s = status.get(device.id, {})
    online = bool(s.get("running"))
    recent = store.get_logs(device_id=device.id, limit=10)
    return {
        **device.to_dict(),
        "last_message_at": _utc(device.last_message_at),
        "online": online,
        "error": s.get("error"),
        "state": _state(device, online, s.get("error")),
        "address": f"{device.host}:{device.port}" if device.mode == "connect" else f"Port {device.port}",
        # Oldest first, for the recent-messages strip
        "recent": [{"status": l.status, "timestamp": _utc(l.timestamp), "lab_test_name": l.lab_test_name}
                   for l in reversed(recent)],
        "last_log": _log(recent[0]) if recent else None,
    }


def _devices():
    status = get_listener_status()
    return [_device(d, status) for d in store.get_devices()]


def _device_or_404(device_id):
    device = store.get_device(device_id)
    if device is None:
        abort(404)
    return _device(device, get_listener_status())


# --------------------------------------------------------------------------- #
#  Pages
# --------------------------------------------------------------------------- #

@pages_bp.route("/")
def dashboard():
    devices = _devices()
    counts = store.log_counts()
    return render_template(
        "dashboard.html",
        devices=devices,
        offline=[d for d in devices if d["is_active"] and not d["online"]],
        stats={
            "total_devices": len(devices),
            "online": sum(1 for d in devices if d["online"]),
            "total_messages": counts["total"],
            "success": counts["success"],
            "failed": counts["failed"],
        },
        logs=[_log(l) for l in store.get_logs(limit=20)],
        erpnext_url=store.get_settings().erpnext_url,
    )


@pages_bp.route("/devices")
def devices():
    return render_template("devices.html", devices=_devices(), lan_ip=lan_ip())


@pages_bp.route("/devices/new")
def new_device():
    taken = {d.port for d in store.get_devices() if d.mode == "listen"}
    port = 5600
    while port in taken:
        port += 1
    return render_template("device_form.html", device=None, suggested_port=port,
                           listen_ports=_listen_ports(), lan_ip=lan_ip())


@pages_bp.route("/devices/<int:device_id>/edit")
def edit_device(device_id):
    device = _device_or_404(device_id)
    return render_template("device_form.html", device=device, suggested_port=device["port"],
                           listen_ports=_listen_ports(exclude=device_id), lan_ip=lan_ip())


def _listen_ports(exclude=None):
    """'5600 (BC-5380), ...' — ports other listen devices already use."""
    return ", ".join(f"{d.port} ({d.name})" for d in store.get_devices()
                     if d.mode == "listen" and d.id != exclude)


@pages_bp.route("/devices/<int:device_id>/logs")
def device_logs(device_id):
    device = _device_or_404(device_id)
    return render_template(
        "device_logs.html",
        device=device,
        logs=[_log(l) for l in store.get_logs(device_id=device_id, limit=50)],
        erpnext_url=store.get_settings().erpnext_url,
        lan_ip=lan_ip(),
    )


@pages_bp.route("/settings")
def settings():
    return render_template("settings.html", settings=store.get_settings())
