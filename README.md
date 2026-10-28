# HL7 Relay — Flask App

A lightweight Flask web app that runs an HL7 MLLP listener for one or more
lab machines and relays each message into ERPNext as a `Lab Machine
Message` record.

Each lab machine is configured as a **device** with its own dedicated MLLP
port, so multiple machines can connect at once — add, edit, activate, or
remove a device from the dashboard and its listener binds/unbinds live,
no restart needed.

## Requirements

- Python 3.9+
- Network access from the lab machines to this host (same LAN or VPN)
- ERPNext instance with API Key/Secret and the `Lab Machine Message` doctype

## Quick Start (one click)

- **Linux:** double-click `setup_linux.sh` (or run `./setup_linux.sh`)
- **Windows:** double-click `setup_windows.bat`

Nothing needs to be installed first. The script:

1. Installs **Python 3.9+** and **Git** if they are missing
   - Windows: via `winget`, falling back to the official python.org / Git for
     Windows installers (per-user, silent)
   - Linux: via the system package manager (`apt`, `dnf`, `yum`, `zypper`,
     `pacman` or `apk`) — you'll be asked for your sudo password
2. Downloads the app (git clone) if the script was run on its own — into
   `~/hl7-relay` on Linux or `%LOCALAPPDATA%\HL7Relay` on Windows
   (override with the `HL7_RELAY_HOME` environment variable)
3. Creates the virtual environment, installs the Python packages, starts the
   app, and opens http://localhost:5151 in your browser

So the setup script alone is enough to install on a fresh machine. On Linux
you can also run:

```bash
curl -fsSL https://raw.githubusercontent.com/Aakvatech-Limited/hl7/version-15-hotfix/setup_linux.sh | bash
```

Equivalent manual command once Python is installed:

```bash
python3 run.py    # Windows: python run.py
```

Optionally copy `.env.example` to `.env` to override defaults before first
run. To serve the dashboard on a different port, set the
`HL7_DASHBOARD_PORT` environment variable before launching.

## Setup Checklist

1. **Settings** → Enter your ERPNext URL, API Key, and API Secret → Save → Test Connection
2. **Devices** → Add each lab machine (name + a unique MLLP port) → point that machine's HL7 interface at this host on that port
3. **Settings** → Install & Run in Background

## How it Works

```
Lab machine (HL7 interface)
    │  MLLP over TCP
    ▼
One listener per device (asyncio, port from Devices config)
    │
    ├── extracts machine_make / machine_model / lab_test_name from MSH/OBR
    ├── POSTs the raw message to ERPNext (/api/resource/Lab Machine Message)
    ├── sends the HL7 ACK back to the machine
    └── records a message log entry (in-memory + logs/error.log on failure)
```

### No database needed

All configuration (ERPNext credentials, devices/ports) lives in a plain
`config.json` next to the app — readable, editable, and easy to back up.
Message history is in-memory and resets on restart; failures are also
appended to `logs/error.log`.

> `config.json` contains your API credentials — keep it out of git
> (it is listed in `.gitignore`).
>
> **Upgrading from the old `hl7_listener.py` script:** on first start
> the app automatically migrates ERPNext credentials from the legacy
> `local_config.py` into `config.json` (creating one "Default" device on
> the old hardcoded port 5600), and keeps the old file as
> `local_config.py.bak`.

## Updates

When installed from a git clone, the app checks
[Aakvatech-Limited/hl7](https://github.com/Aakvatech-Limited/hl7)
(`version-15-hotfix`) on startup and every 6 hours. If new commits exist, a
popup lists them with an **Update now** button, which fast-forwards the clone,
reinstalls dependencies when `requirements.txt` changed, and restarts the app.
Settings (`config.json`, `.env`) are untouched, and lab machines reconnect on
their own once the listeners are back up. **Later** hides the popup until a
newer commit lands; the sidebar badge and **Settings → App Updates** stay
available.

The update is refused without changing files while a message is being relayed
to ERPNext, when tracked files have local edits, or when the clone has its own
commits. Zip installs do not expose automatic updates.

## Project Structure

```
hl7/
├── app/
│   ├── __init__.py               Flask app factory
│   ├── store.py                  config.json persistence + in-memory message log
│   ├── routes/
│   │   ├── pages.py              The web pages (dashboard, devices, settings)
│   │   ├── ui_api.py             JSON actions the page buttons call (/api/ui/*)
│   │   └── api.py                JSON API (status, service management, updates)
│   ├── services/
│   │   ├── hl7_listener.py       Multi-device MLLP listener manager (asyncio)
│   │   ├── erpnext_service.py    ERPNext REST API client
│   │   └── updater.py            Git-based update checks + safe apply
│   ├── templates/                Jinja2 page templates (_ui.html holds shared pieces)
│   └── static/                   frappe.css, app.js, icons.svg, font, favicon
├── config.py
├── run.py                        Auto-bootstrap launcher (venv + deps + start)
├── server.py                     Flask server entry point
├── service_manager.py            OS service install (systemd / Task Scheduler)
├── setup_linux.sh                One-click setup (Linux)
├── setup_windows.bat             One-click setup (Windows)
└── requirements.txt
```

## Web Interface

The interface at `http://localhost:5151` looks like Frappe v16 but uses no
framework and has no build step: Flask renders the pages from
`app/templates`, styled by one stylesheet (`app/static/frappe.css`, which
carries frappe-ui's colour tokens for light and dark mode) and made
interactive by one small script (`app/static/app.js`). Icons are
[Lucide](https://lucide.dev) symbols in `app/static/icons.svg`; the font is
Inter, cut down to Latin characters.

To change the interface, edit those files and reload the page. No Node.js,
npm or build is ever needed. To add an icon, copy its `<path>` elements from
lucide.dev into a new `<symbol id="name">` in `icons.svg`.

## Running in Production (background service)

The app installs itself as an OS-level background service directly from the UI:

1. Launch the app with the setup script (see Quick Start).
2. Open **Settings** → **Background Service** → click **Install & Run in Background**.
3. The foreground window hands off to the background service within a few seconds.
   You can now close the terminal — the app keeps running and auto-starts on boot.

Under the hood:

- **Linux:** a systemd *user* service (`~/.config/systemd/user/hl7-erpnext-sync.service`,
  no sudo required) with `Restart=on-failure` and `loginctl enable-linger` so it also
  runs while you're logged out. Output is appended to `hl7_relay.log`.
- **Windows:** a Task Scheduler task (`ERPNextHL7SyncService`) that starts at logon
  using `pythonw.exe` (no console window).

To disable auto-start, use **Settings → Background Service → Remove from Auto-Start**.

### Useful commands (Linux)

```bash
# Service status
systemctl --user status hl7-erpnext-sync

# Watch live logs
tail -f hl7_relay.log

# Restart / stop the service manually
systemctl --user restart hl7-erpnext-sync
systemctl --user stop hl7-erpnext-sync
```

## Known limitation

This relay only creates the raw `Lab Machine Message` record in ERPNext —
it does not parse OBX result segments into `Lab Test` records itself
(that previously happened in this repo's now-removed Frappe doctype
controller). Confirm that logic still exists server-side (e.g. in the
`hms_tz` app) on the ERPNext instance this relay points at, otherwise lab
results will stop flowing into `Lab Test` records.
