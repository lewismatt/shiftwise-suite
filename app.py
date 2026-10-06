"""ShiftWise top-level application entry point and backward compatibility layer.

Provides the primary WSGI application object 'app' for Gunicorn/Caddy and
re-exports all domain functions, database helpers, and constants so existing
tests, scripts, and deployment configurations continue to work without modification.
"""

import importlib
import os
import smtplib
import sys
import types
import urllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

from shiftwise import app, create_app
from shiftwise.auth import login_required
from shiftwise.db import (
    POSTGRES_SCHEMA,
    UNIQUE_VIOLATION_ERRORS,
    database_engine,
    db,
    execute_sql,
    init_db,
    lock_shifts,
    lock_users,
    monday_of,
    read_queries,
    try_scheduler_lock,
)
from shiftwise.domain.constants import DAYS, MIN_DAYS_OFF
from shiftwise.domain.rules import (
    assignment_block_reason,
    priority_key,
    shift_hours,
    unavailable_uids,
    valid_email,
    valid_phone,
)
from shiftwise.domain.swaps import reconcile_swap_invites
from shiftwise.notify import notify, notify_many, send_schedule_link
from shiftwise.routes.calendar import calendar_days
from shiftwise.scheduler.coverage import apply_sick, coverage_plan
from shiftwise.scheduler.debounce import enqueue_rebuild, process_rebuilds, request_rebuild
from shiftwise.scheduler.engine import run_scheduler

# Keep compatibility writes here; delivery code depends only on its own module.
_notification_patch_targets = {
    "smtplib": (importlib.import_module("shiftwise.notify"), "_smtplib"),
    "urllib": (importlib.import_module("shiftwise.notify"), "_urllib"),
    "send_schedule_link": (
        importlib.import_module("shiftwise.routes.roster"),
        "send_schedule_link",
    ),
}


class _AppModule(types.ModuleType):
    """Forward legacy notification patches to their consumer modules."""

    def __setattr__(self, name, val):
        if name in _notification_patch_targets:
            module, attribute = _notification_patch_targets[name]
            setattr(module, attribute, val)
        super().__setattr__(name, val)


sys.modules[__name__].__class__ = _AppModule


__all__ = [
    "create_app",
    "app",
    "db",
    "database_engine",
    "execute_sql",
    "read_queries",
    "init_db",
    "monday_of",
    "lock_shifts",
    "lock_users",
    "try_scheduler_lock",
    "enqueue_rebuild",
    "process_rebuilds",
    "request_rebuild",
    "UNIQUE_VIOLATION_ERRORS",
    "POSTGRES_SCHEMA",
    "DAYS",
    "MIN_DAYS_OFF",
    "login_required",
    "notify",
    "notify_many",
    "send_schedule_link",
    "run_scheduler",
    "coverage_plan",
    "apply_sick",
    "shift_hours",
    "assignment_block_reason",
    "priority_key",
    "unavailable_uids",
    "reconcile_swap_invites",
    "valid_email",
    "valid_phone",
    "calendar_days",
    "check_password_hash",
    "generate_password_hash",
    "date",
    "datetime",
    "timedelta",
    # Re-exported for the monkeypatch facade contract (AGENTS.md §2):
    # legacy tests may patch app.smtplib.SMTP / app.urllib.request.urlopen.
    "smtplib",
    "urllib",
]


if __name__ == "__main__":
    init_db(seed_demo=os.environ.get("SHIFTWISE_DEMO_SEED") in ("1", "8", "mock"))
    app.run(
        host=os.environ.get("SHIFTWISE_HOST", "127.0.0.1"),
        port=int(os.environ.get("SHIFTWISE_PORT", "5000")),
        debug=False,
    )
