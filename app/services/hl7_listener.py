"""
Multi-device MLLP listener manager.

Each active device runs in one of two modes, all inside a single
background asyncio event loop (one daemon thread):

  - listen:  the analyzer is the TCP client (BC-760, BS-230, CL-900i, ...).
             We run an HL7 MLLP server on the device's port and ACK each
             message.
  - connect: the analyzer is the TCP server (BS-240, BS-430, ...). We
             connect out to its host and port, read MLLP frames, and
             reconnect whenever the link drops. No ACK is sent, matching
             the field-tested server-mode script.

Devices can be added, edited, (de)activated, or deleted from the Devices
page and the corresponding listener/connection is started or stopped
live, with no app restart required.

Message handling ports the logic of the original ``hl7_listener.py``
script (positional field extraction from the MSH/OBR segments), then
relays each message to ERPNext via ``app.services.erpnext_service``.
"""
import asyncio
import contextlib
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

_servers: dict = {}    # device_id -> asyncio.Server        (listen devices)
_clients: dict = {}    # device_id -> asyncio.Task          (connect devices)
_connected: set = set()  # connect devices with an open connection right now
_errors: dict = {}     # device_id -> last bind/connect error string
_loop = None
_flask_app = None
_error_logger = None
_error_logger_path = None
_in_flight = 0         # messages currently being relayed to ERPNext
_in_flight_lock = threading.Lock()

MLLP_START = b"\x0b"
MLLP_END = b"\x1c\x0d"
CONNECT_TIMEOUT = 10        # seconds
RECONNECT_MIN = 5           # seconds; doubles on each failed attempt ...
RECONNECT_MAX = 60          # ... up to this
FRAME_LIMIT = 4 * 1024 * 1024   # Mindray results can carry base64 histogram images


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
    if device.mode == "connect":
        _clients[device.id] = asyncio.create_task(_connect_loop(app, device))
        return
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
    task = _clients.pop(device_id, None)
    if server is not None:
        server.close()
        await server.wait_closed()
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    _connected.discard(device_id)
    _errors.pop(device_id, None)


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
    """{device_id: {"running", "mode", "host", "port", "error"}} for the UI.

    "running" means listening (listen mode) or connected (connect mode).
    """
    status = {}
    for device in store.get_devices():
        status[device.id] = {
            "running": device.id in _servers or device.id in _connected,
            "mode": device.mode,
            "host": device.host,
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


async def _handle_message(app, device_id, str_hl7_message) -> None:
    """Extract the routing fields and relay one message to ERPNext (both modes)."""
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


async def process_hl7_messages(app, device_id, reader, writer) -> None:
    """Called for every socket connection to this device's MLLP server."""
    peername = writer.get_extra_info("peername")
    logger.info("Connection established %s (device %s)", peername, device_id)
    try:
        while not writer.is_closing():
            hl7_message = await reader.readmessage()
            await _handle_message(app, device_id, str(hl7_message).replace("\r", "\n"))

            writer.writemessage(hl7_message.create_ack())
            await writer.drain()
    except asyncio.IncompleteReadError:
        if not writer.is_closing():
            writer.close()
            await writer.wait_closed()
    logger.info("Connection closed %s (device %s)", peername, device_id)


async def _connect_loop(app, device) -> None:
    """Keep a connection open to a server-mode analyzer, reconnecting when it drops.

    Runs until cancelled by _stop_one().
    """
    delay = RECONNECT_MIN
    while True:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(device.host, device.port, limit=FRAME_LIMIT),
                timeout=CONNECT_TIMEOUT,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            _errors[device.id] = str(exc) or f"No answer from {device.host}:{device.port}"
            logger.warning("Cannot connect to '%s' at %s:%d: %s (retrying in %ds)",
                           device.name, device.host, device.port, _errors[device.id], delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)
            continue

        delay = RECONNECT_MIN
        _errors.pop(device.id, None)
        _connected.add(device.id)
        logger.info("Connected to '%s' at %s:%d", device.name, device.host, device.port)
        try:
            await _read_frames(app, device.id, reader)
            _errors[device.id] = "The machine closed the connection."
        except (OSError, asyncio.LimitOverrunError, ValueError) as exc:
            _errors[device.id] = f"Connection lost: {exc}"
        finally:
            _connected.discard(device.id)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        logger.warning("Disconnected from '%s': %s (reconnecting in %ds)",
                       device.name, _errors[device.id], RECONNECT_MIN)
        await asyncio.sleep(RECONNECT_MIN)


async def _read_frames(app, device_id, reader) -> None:
    """Read MLLP frames (<VT> message <FS><CR>) until the machine closes the connection."""
    while True:
        try:
            frame = await reader.readuntil(MLLP_END)
        except asyncio.IncompleteReadError:
            return  # connection closed; any partial frame is dropped
        raw = frame[:-len(MLLP_END)].lstrip(MLLP_START)
        if not raw.strip():
            continue
        await _handle_message(app, device_id, raw.decode("utf-8", errors="replace").replace("\r", "\n"))


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
