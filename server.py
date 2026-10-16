"""
Flask server entry point for HL7 Relay.

Runs the app factory, starts the background MLLP listener manager (one
listener per configured device) and the 6-hourly update check, and serves
the dashboard on http://localhost:5050.

Normally launched via ``run.py`` (foreground) or the installed
background service (systemd / Task Scheduler).
"""
import logging
import os

from app import create_app
from app.services.hl7_listener import start_listener_background
from app.services.updater import start_update_checker

IS_BACKGROUND = os.environ.get("HL7_SERVICE_MODE") == "background"
PORT = int(os.environ.get("HL7_DASHBOARD_PORT", 5050))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Suppress noisy werkzeug request logs when running as a background service
if IS_BACKGROUND:
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

app = create_app()

if __name__ == "__main__":
    start_listener_background(app)
    start_update_checker()
    app.run(host="0.0.0.0", port=PORT, debug=False)
