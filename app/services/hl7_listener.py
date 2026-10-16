"""
Multi-device MLLP listener manager.

Runs one HL7 MLLP TCP server per active device, each bound to its own
configured port, all inside a single background asyncio event loop
(one daemon thread). Devices can be added, edited, (de)activated, or
deleted from the Devices page and the corresponding listener is bound
or unbound live, with no app restart required.

Message handling ports the logic of the original ``hl7_listener.py``
script (positional field extraction from the MSH/OBR segments), then
relays each message to ERPNext via ``app.services.erpnext_service``.
"""
import asyncio
import functools
import logging
import os
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler

from hl7.mllp import start_hl7_server

from app import store
from app.services.erpnext_service import ERPNextClient

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

_servers: dict = {}    # device_id -> asyncio.Server
_errors: dict = {}     # device_id -> last bind error string
_loop = None
_flask_app = None
_error_logger = None
_error_logger_path = None
_in_flight = 0         # messages currently being relayed to ERPNext
_in_flight_lock = threading.Lock()


# --------------------------------------------------------------------------- #
#  Lifecycle
# --------------------------------------------------------------------------- #

def start_listener_background(app) -> None:
    """Start the listener manager in a background daemon thread."""
    global _flask_app
    _flask_app = app
    thread = threading.Thread(target=lambda: asyncio.run(_run(app)), daemon=True, name="hl7-listener")
    thread.start()


async def _run(app) -> None:
    global _loop
    _loop = asyncio.get_running_loop()
    for device in store.get_devices(active_only=True):
        await _start_one(app, device)
    await asyncio.Event().wait()  # idle forever; keeps the event loop (and servers) alive


async def _start_one(app, device) -> None:
    try:
        server = await start_hl7_server(
            functools.partial(process_hl7_messages, app, device.id),
            port=device.port,
        )
        _servers[device.id] = server
        _errors.pop(device.id, None)
        logger.info("HL7 listener started for '%s' on port %d", device.name, device.port)
    except OSError as exc:
        _errors[device.id] = str(exc)
        logger.error("Failed to bind HL7 listener for '%s' on port %d: %s", device.name, device.port, exc)


async def _stop_one(device_id) -> None:
    server = _servers.pop(device_id, None)
    _errors.pop(device_id, None)
    if server is not None:
        server.close()
        await server.wait_closed()


def reload_device(device_id) -> None:
    """(Re)bind the listener for one device — call after adding/editing a device."""
    if _loop is None:
        return  # manager hasn't started yet; it will pick up the device on startup
    asyncio.run_coroutine_threadsafe(_reload_one(device_id), _loop)


async def _reload_one(device_id) -> None:
    await _stop_one(device_id)
    device = store.get_device(device_id)
    if device and device.is_active:
        await _start_one(_flask_app, device)


def stop_device(device_id) -> None:
    """Unbind the listener for one device — call after deleting/deactivating a device."""
    if _loop is None:
        return
    asyncio.run_coroutine_threadsafe(_stop_one(device_id), _loop)


def get_in_flight() -> int:
    """Number of messages being relayed right now (the updater waits for 0)."""
    return _in_flight


def get_listener_status() -> dict:
    """{device_id: {"running": bool, "port": int, "error": str|None}} for the UI."""
    status = {}
    for device in store.get_devices():
        status[device.id] = {
            "running": device.id in _servers,
            "port": device.port,
            "error": _errors.get(device.id),
        }
    return status


# --------------------------------------------------------------------------- #
#  Message handling
# --------------------------------------------------------------------------- #

def _safe_field(lines, line_idx, field_idx):
    try:
        return lines[line_idx].split("|")[field_idx]
    except IndexError:
        return None


async def process_hl7_messages(app, device_id, reader, writer) -> None:
    """Called for every socket connection to this device's MLLP server."""
    peername = writer.get_extra_info("peername")
    logger.info("Connection established %s (device %s)", peername, device_id)
    try:
        while not writer.is_closing():
            hl7_message = await reader.readmessage()
            str_hl7_message = str(hl7_message).replace("\r", "\n")
            msg_lines = str_hl7_message.splitlines()

            machine_make = _safe_field(msg_lines, 0, 3)
            machine_model = _safe_field(msg_lines, 0, 2)
            lab_test_name = _safe_field(msg_lines, 3, 3)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, _relay, app, device_id,
                machine_make, machine_model, lab_test_name, str_hl7_message, timestamp,
            )

            writer.writemessage(hl7_message.create_ack())
            await writer.drain()
    except asyncio.IncompleteReadError:
        if not writer.is_closing():
            writer.close()
            await writer.wait_closed()
    logger.info("Connection closed %s (device %s)", peername, device_id)


def _get_error_logger(logs_directory):
    """Rotating error.log under the configured logs directory (recreated if the path changes)."""
    global _error_logger, _error_logger_path
    log_dir = logs_directory if os.path.isabs(logs_directory) else os.path.join(BASE_DIR, logs_directory)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "error.log")

    if _error_logger is None or _error_logger_path != log_file:
        error_logger = logging.getLogger("hl7_relay.error")
        for h in list(error_logger.handlers):
            error_logger.removeHandler(h)
        handler = RotatingFileHandler(log_file, maxBytes=10_000_000, backupCount=50)
        handler.setFormatter(logging.Formatter("%(asctime)s\t%(levelname)s\t%(message)s"))
        error_logger.addHandler(handler)
        error_logger.setLevel(logging.ERROR)
        _error_logger = error_logger
        _error_logger_path = log_file

    return _error_logger


def _relay(app, device_id, machine_make, machine_model, lab_test_name, message, timestamp) -> None:
    """Runs in a thread executor (blocking requests call) — reads fresh settings each time."""
    global _in_flight
    with _in_flight_lock:
        _in_flight += 1
    try:
        _relay_one(app, device_id, machine_make, machine_model, lab_test_name, message, timestamp)
    finally:
        with _in_flight_lock:
            _in_flight -= 1


def _relay_one(app, device_id, machine_make, machine_model, lab_test_name, message, timestamp) -> None:
    with app.app_context():
        device = store.get_device(device_id)
        device_name = device.name if device else "Unknown"
        settings = store.get_settings()
        error_logger = _get_error_logger(settings.logs_directory)

        try:
            client = ERPNextClient(settings.erpnext_url, settings.api_key, settings.api_secret)
            erpnext_name = client.send_lab_machine_message(
                machine_make, machine_model, lab_test_name, message, timestamp
            )
            store.add_log(
                device_id, device_name, "Success",
                machine_make=machine_make, machine_model=machine_model,
                lab_test_name=lab_test_name, erpnext_name=erpnext_name,
            )
        except Exception as exc:
            error_str = str(exc)
            error_logger.error("\t".join([
                "Error relaying HL7 message.", device_name, timestamp,
                str(lab_test_name), error_str,
            ]))
            store.add_log(
                device_id, device_name, "Failed",
                machine_make=machine_make, machine_model=machine_model,
                lab_test_name=lab_test_name, error=error_str,
            )

        if device:
            store.touch_device(device_id)
