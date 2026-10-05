"""PostgreSQL configuration failures must stop application startup."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("engine", "url", "message"),
    [
        (None, None, "PostgreSQL is required. Set DATABASE_URL"),
        ("postgres", "", "PostgreSQL is required. Set DATABASE_URL"),
        ("sqlite", None, "SQLite support removed — configure PostgreSQL via DATABASE_URL"),
        (
            "sqlite",
            "postgresql://user:secret@127.0.0.1:1/unused",
            "SQLite support removed — configure PostgreSQL via DATABASE_URL",
        ),
        (None, "mysql://user:secret@127.0.0.1/scheduler", "DATABASE_URL must be a PostgreSQL URL"),
        (
            None,
            "postgresql://user:secret@127.0.0.1:1/unused?connect_timeout=1",
            "PostgreSQL is unavailable. Check DATABASE_URL",
        ),
    ],
)
def test_application_import_rejects_unusable_database(engine, url, message):
    environment = os.environ.copy()
    environment.pop("SHIFTWISE_DB_ENGINE", None)
    environment.pop("DATABASE_URL", None)
    if engine is not None:
        environment["SHIFTWISE_DB_ENGINE"] = engine
    if url is not None:
        environment["DATABASE_URL"] = url
    result = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=Path(__file__).resolve().parent.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert "secret@" not in result.stderr
