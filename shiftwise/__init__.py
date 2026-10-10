"""ShiftWise scheduling application package."""

from pathlib import Path

from flask import Flask, g, session

from shiftwise.auth import login_required
from shiftwise.config import Config
from shiftwise.db import POSTGRES_SCHEMA, db, init_db, monday_of
from shiftwise.domain.constants import DAYS, MIN_DAYS_OFF
from shiftwise.domain.rules import (
    assignment_block_reason,
    priority_key,
    shift_hours,
    unavailable_uids,
)
from shiftwise.notify import notify
from shiftwise.presentation import format_clock
from shiftwise.routes import register_blueprints
from shiftwise.scheduler.coverage import apply_sick, coverage_plan
from shiftwise.scheduler.debounce import init_app as init_scheduler
from shiftwise.scheduler.engine import run_scheduler
from shiftwise.security import init_app as init_csrf
from shiftwise.themes import PALETTES_BY_ID, css_variables

ROOT_DIR = Path(__file__).resolve().parent.parent


def create_app(config_class=Config):
    """Application factory for ShiftWise."""
    # Validate configuration and connectivity before serving any requests.
    conn = db()
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    flask_app = Flask(
        __name__,
        template_folder=str(ROOT_DIR / "templates"),
        static_folder=str(ROOT_DIR / "static"),
    )
    flask_app.secret_key = config_class.SECRET_KEY

    if config_class.BEHIND_PROXY:
        from werkzeug.middleware.proxy_fix import ProxyFix

        flask_app.wsgi_app = ProxyFix(
            flask_app.wsgi_app,
            x_for=1,
            x_proto=1,
            x_host=1,
            x_prefix=1,
        )

    flask_app.config["SESSION_COOKIE_HTTPONLY"] = config_class.SESSION_COOKIE_HTTPONLY
    flask_app.config["SESSION_COOKIE_SAMESITE"] = config_class.SESSION_COOKIE_SAMESITE
    if config_class.SESSION_COOKIE_SECURE:
        flask_app.config["SESSION_COOKIE_SECURE"] = True

    init_csrf(flask_app)
    init_scheduler(flask_app)
    register_blueprints(flask_app)

    flask_app.jinja_env.filters["clock_time"] = format_clock

    @flask_app.context_processor
    def inject_time_format():
        time_format = "24h"
        background_palette = "shiftwise"
        unread_notifications = 0
        uid = session.get("uid")
        if uid:
            user = getattr(g, "current_user", None)
            conn = db()
            try:
                if user is None:
                    user = conn.execute(
                        "SELECT time_format, background_palette, role FROM users WHERE id=?",
                        (uid,),
                    ).fetchone()
                unread_notifications = conn.execute(
                    "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=FALSE",
                    (uid,),
                ).fetchone()[0]
            finally:
                conn.close()
            if user and user["time_format"] in ("12h", "24h"):
                time_format = user["time_format"]
            if user and user["role"] == "employee" and user["background_palette"] in PALETTES_BY_ID:
                background_palette = user["background_palette"]
        return {
            "time_format": time_format,
            "background_palette": background_palette,
            "background_palette_css": css_variables(background_palette),
            "unread_notifications": unread_notifications,
        }

    return flask_app


# Default application instance for WSGI servers and tests
app = create_app()

__all__ = [
    "create_app",
    "app",
    "init_db",
    "monday_of",
    "POSTGRES_SCHEMA",
    "DAYS",
    "MIN_DAYS_OFF",
    "login_required",
    "notify",
    "run_scheduler",
    "coverage_plan",
    "apply_sick",
    "shift_hours",
    "assignment_block_reason",
    "priority_key",
    "unavailable_uids",
]
