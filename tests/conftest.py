"""Pytest configuration and shared fixtures for ShiftWise test suite."""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_support import (
    isolate_database,
    postgres_engine_active,
    truncate_postgres_tables,
)

import app as appmod
from shiftwise.security import use_csrf_aware_test_client

# Test clients attach a valid CSRF token to unsafe requests, so the suite
# exercises the real check (see tests/test_csrf.py) instead of disabling it.
use_csrf_aware_test_client(appmod.app)


def pytest_collection_modifyitems(config, items):
    """Skip SQLite-only tests when the suite runs against PostgreSQL."""
    if not postgres_engine_active():
        return
    skip = pytest.mark.skip(reason="SQLite-only test; not applicable to PostgreSQL")
    for item in items:
        if item.get_closest_marker("sqlite_only"):
            item.add_marker(skip)


@pytest.fixture
def isolated_db():
    """Provides a fresh isolated database for testing."""
    temp_dir = isolate_database(appmod)
    yield appmod
    temp_dir.cleanup()


@pytest.fixture(autouse=True)
def disable_scheduler_worker(monkeypatch):
    # Tests explicitly drive queued work; do not let a poller outlive a test DB.
    monkeypatch.setitem(appmod.app.config, "SCHEDULER_WORKER_ENABLED", False)


@pytest.fixture(scope="session")
def _postgres_schema_ready():
    """Create the shared PostgreSQL schema once so per-test TRUNCATE has tables."""
    if postgres_engine_active():
        appmod.init_db(seed_demo=True)
    yield


@pytest.fixture(autouse=True)
def _postgres_test_isolation(request, _postgres_schema_ready):
    """Reset shared PostgreSQL tables around each test.

    SQLite tests get file-per-test isolation via ``isolate_database``; on a
    shared PostgreSQL database that DB_PATH swap is a no-op, so every test
    instead starts and ends with empty tables. Modules that own their
    isolation (phase3 shared-table cleanup, phase4 throwaway schema) opt out
    with ``@pytest.mark.pg_own_isolation``.
    """
    if not postgres_engine_active() or request.node.get_closest_marker("pg_own_isolation"):
        yield
        return
    _truncate_postgres()
    yield
    _truncate_postgres()


def _truncate_postgres():
    conn = appmod.db()
    try:
        truncate_postgres_tables(conn)
    finally:
        conn.close()
