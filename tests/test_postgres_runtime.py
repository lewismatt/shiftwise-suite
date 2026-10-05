"""PostgreSQL configuration failures must stop application startup."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from shiftwise.db import _database_engine, _resolve_database_url


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


# --- POSTGRES_PASSWORD_FILE (Docker secrets) -----------------------------------


def _write_secret(tmp_path, name, content):
    secret = tmp_path / name
    secret.write_text(content, encoding="utf-8")
    return str(secret)


def _clear_db_env(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD_FILE", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SHIFTWISE_DB_ENGINE", raising=False)
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)


def test_secret_file_assembles_database_url(tmp_path, monkeypatch):
    _clear_db_env(monkeypatch)
    secret = _write_secret(tmp_path, "pw", "p@ss:w#rd?x\n")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", secret)
    monkeypatch.setenv("POSTGRES_USER", "app_user")
    monkeypatch.setenv("POSTGRES_DB", "app_db")
    url = _resolve_database_url()
    assert url == ("postgresql://app_user:p%40ss%3Aw%23rd%3Fx@postgres:5432/app_db")


def test_secret_file_uses_shiftwise_defaults(tmp_path, monkeypatch):
    _clear_db_env(monkeypatch)
    secret = _write_secret(tmp_path, "pw", "s3cret")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", secret)
    url = _resolve_database_url()
    assert url == "postgresql://shiftwise:s3cret@postgres:5432/shiftwise"


def test_secret_file_ignores_database_url(tmp_path, monkeypatch):
    _clear_db_env(monkeypatch)
    secret = _write_secret(tmp_path, "pw", "from-secret")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", secret)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:stale@dbhost:5432/other")
    url = _resolve_database_url()
    assert "from-secret" in url
    assert "stale" not in url


def test_secret_file_missing_fails_fast(tmp_path, monkeypatch):
    _clear_db_env(monkeypatch)
    missing = str(tmp_path / "does-not-exist")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", missing)
    with pytest.raises(RuntimeError, match="unreadable file"):
        _resolve_database_url()


@pytest.mark.parametrize("content", ["", "\n"])
def test_secret_file_empty_fails_fast(tmp_path, monkeypatch, content):
    _clear_db_env(monkeypatch)
    secret = _write_secret(tmp_path, "pw", content)
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", secret)
    with pytest.raises(RuntimeError, match="empty"):
        _resolve_database_url()


def test_no_secret_file_uses_database_url_verbatim(monkeypatch):
    _clear_db_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "  postgresql://u:p@h:5432/d  ")
    assert _resolve_database_url() == "postgresql://u:p@h:5432/d"


def test_database_engine_accepts_secret_mode(tmp_path, monkeypatch):
    _clear_db_env(monkeypatch)
    secret = _write_secret(tmp_path, "pw", "s3cret\n")
    monkeypatch.setenv("POSTGRES_PASSWORD_FILE", secret)
    assert _database_engine() == "postgres"
