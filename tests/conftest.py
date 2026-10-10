"""Pytest configuration and shared fixtures for ShiftWise test suite."""

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_support import truncate_postgres_tables

# Choose the destructive test target before importing the application: app import
# opens a connection, and schema initialization / TRUNCATE use DATABASE_URL.
_test_url = os.environ.get("TEST_DATABASE_URL")
if _test_url is not None:
    if not _test_url.strip():
        raise pytest.UsageError("TEST_DATABASE_URL must identify a disposable PostgreSQL database.")
    os.environ["DATABASE_URL"] = _test_url.strip()
    # The runtime secret-file override otherwise ignores DATABASE_URL entirely.
    os.environ.pop("POSTGRES_PASSWORD_FILE", None)
elif os.environ.get("POSTGRES_PASSWORD_FILE"):
    raise pytest.UsageError(
        "Set TEST_DATABASE_URL to a disposable database when POSTGRES_PASSWORD_FILE is configured."
    )

import app as appmod
from shiftwise.security import use_csrf_aware_test_client

# Test clients attach a valid CSRF token to unsafe requests, so the suite
# exercises the real check (see tests/test_csrf.py) instead of disabling it.
use_csrf_aware_test_client(appmod.app)


@pytest.fixture(autouse=True)
def disable_scheduler_worker(_postgres_test_isolation, monkeypatch):
    # Resolve isolation before monkeypatch so database URL overrides are undone
    # before the shared-table teardown runs.
    # Tests explicitly drive queued work; do not let a poller outlive a test DB.
    monkeypatch.setitem(appmod.app.config, "SCHEDULER_WORKER_ENABLED", False)


@pytest.fixture(scope="session")
def _postgres_schema_ready():
    """Create the shared PostgreSQL schema once so per-test TRUNCATE has tables."""
    appmod.init_db(seed_demo=True)
    yield


@pytest.fixture(autouse=True)
def _postgres_test_isolation(_postgres_schema_ready):
    """Reset all shared PostgreSQL tables before and after each test.

    Set up before fixtures that monkeypatch the database URL so teardown runs
    after they restore the shared database, including throwaway-schema tests.
    """
    _truncate_postgres()
    yield
    _truncate_postgres()


def _truncate_postgres():
    conn = appmod.db()
    try:
        truncate_postgres_tables(conn)
    finally:
        conn.close()
