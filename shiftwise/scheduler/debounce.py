"""Durable PostgreSQL rebuild requests shared by all web processes."""

import logging
import threading

from shiftwise.db import db, try_scheduler_lock
from shiftwise.scheduler.engine import _schedule_week

log = logging.getLogger(__name__)


def enqueue_rebuild(conn, week):
    """Save a trailing 500ms deadline in the same transaction as the picks."""
    conn.execute(
        "INSERT INTO scheduler_rebuilds (week_start, due_at) "
        "VALUES (?, clock_timestamp() + interval '500 milliseconds') "
        "ON CONFLICT (week_start) DO UPDATE SET due_at=excluded.due_at, "
        "generation=scheduler_rebuilds.generation + 1",
        (week,),
    )


def request_rebuild(week, actor="system"):
    """Request an automatic rebuild; callers must not use a processed-pick count.

    PostgreSQL commits queued work and returns 0 as an enqueue sentinel. The
    scheduler has not run yet, so this is not a count of processed picks.
    Call run_scheduler() directly when a completed rebuild or its count is
    required, as the explicit manager rebuild and CLI entrypoints do.
    """
    conn = db()
    try:
        enqueue_rebuild(conn, week)
        conn.commit()
    finally:
        conn.close()
    return 0


def process_rebuilds():
    """Run settled weeks once across processes, retaining failures for retry.

    Do not lock the queue row while computing: picks must be able to advance
    its generation. Delete only the generation this run observed, so a change
    arriving during computation schedules another run. The week advisory lock
    protects both the rebuild and acknowledgment until commit.
    """
    conn = db()
    try:
        weeks = conn.execute(
            "SELECT week_start FROM scheduler_rebuilds "
            "WHERE due_at<=clock_timestamp() ORDER BY week_start"
        ).fetchall()
    finally:
        conn.close()
    completed = 0
    for row in weeks:
        week = row["week_start"]
        conn = db()
        try:
            if not try_scheduler_lock(conn, week):
                continue
            pending = conn.execute(
                "SELECT generation FROM scheduler_rebuilds "
                "WHERE week_start=? AND due_at<=clock_timestamp()",
                (week,),
            ).fetchone()
            if not pending:
                continue
            _schedule_week(conn, week)
            conn.execute(
                "DELETE FROM scheduler_rebuilds WHERE week_start=? AND generation=?",
                (week, pending["generation"]),
            )
            conn.commit()
            completed += 1
        finally:
            conn.close()
    return completed


class RebuildWorker:
    """One poller per web process; database locks arbitrate between pollers."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=35)

    def _run(self):
        while not self._stop.wait(0.1):
            try:
                process_rebuilds()
            except Exception:
                log.exception("Scheduler rebuild failed; pending work will be retried")
                self._stop.wait(1)


def init_app(app):
    worker = RebuildWorker()
    app.extensions["scheduler_worker"] = worker

    @app.before_request
    def start_worker():
        # Start after Gunicorn forks and after startup schema initialization.
        # Health checks also resume durable work after a process restart.
        if app.config.get("SCHEDULER_WORKER_ENABLED", True):
            worker.start()
