"""
JSON endpoints for listener status, the background service and updates.
"""
import logging
import os

from flask import Blueprint, jsonify
from app.services.hl7_listener import get_listener_status

api_bp = Blueprint("api", __name__)

logger = logging.getLogger(__name__)

IS_BACKGROUND = os.environ.get("HL7_SERVICE_MODE") == "background"


@api_bp.route("/status")
def status():
    return jsonify(get_listener_status())


@api_bp.route("/service/start", methods=["POST"])
def service_start():
    """Install the OS-level background service (systemd / Task Scheduler).

    When running in the foreground (via run.py), also hand off: this
    process exits after a short delay so the background service can take
    over the dashboard port.
    """
    import service_manager

    try:
        result = service_manager.install_service()
        svc_msg = result.get("message", "")
    except Exception as exc:
        logger.exception("Background service install failed.")
        return jsonify({"success": False, "error": str(exc)}), 500

    if not IS_BACKGROUND:
        import threading

        def _handoff():
            import time
            time.sleep(3)  # give the HTTP response time to reach the browser
            logger.info("Handing off to background service...")
            service_manager.start_background_service()
            time.sleep(1)
            os._exit(0)

        threading.Thread(target=_handoff, daemon=True).start()

        return jsonify({
            "success": True,
            "handoff": True,
            "message": f"{svc_msg} This setup window will hand off to the "
                       "background service in a few seconds.",
        })

    return jsonify({"success": True, "handoff": False, "message": svc_msg})


@api_bp.route("/service/stop", methods=["POST"])
def service_stop():
    """Remove the background service and disable auto-start."""
    import service_manager

    try:
        result = service_manager.uninstall_service()
        msg = result.get("message", "")
        if IS_BACKGROUND:
            import threading

            def _self_stop():
                import time
                time.sleep(3)
                logger.info("Background service uninstalled — exiting.")
                os._exit(0)

            threading.Thread(target=_self_stop, daemon=True).start()
            msg += " This running instance will stop in a few seconds."
        return jsonify({"success": True, "message": msg})
    except Exception as exc:
        logger.exception("Background service uninstall failed.")
        return jsonify({"success": False, "error": str(exc)}), 500


@api_bp.route("/service/info")
def service_info():
    """Return OS-level background service status."""
    import service_manager

    status = service_manager.get_service_status()
    status["background_mode"] = IS_BACKGROUND
    return jsonify(status)


@api_bp.route("/update/status")
def update_status():
    from app.services.updater import get_status
    return jsonify(get_status())


@api_bp.route("/update/check", methods=["POST"])
def update_check():
    from app.services.updater import check_for_updates
    return jsonify(check_for_updates())


@api_bp.route("/update/apply", methods=["POST"])
def update_apply():
    """Apply the latest official update, then restart this process."""
    from app.services.updater import apply_update, restart_app

    try:
        result = apply_update()
    except Exception as exc:
        logger.exception("Update failed.")
        return jsonify({"success": False, "message": str(exc)}), 500

    if result.get("success"):
        restart_app()
        return jsonify({**result, "restarting": True})
    return jsonify(result), 409 if result.get("refused") else 500
