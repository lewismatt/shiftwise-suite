import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


#: Application tables reset between tests on a shared PostgreSQL database.
#: Order is irrelevant: TRUNCATE clears them in one CASCADE statement.
POSTGRES_TABLES = (
    "scheduler_rebuilds",
    "picks",
    "coverage_preferences",
    "assignments",
    "notifications",
    "requests",
    "login_attempts",
    "shifts",
    "users",
)


def postgres_engine_active():
    """True when the environment selects PostgreSQL (mirrors app selection)."""
    import importlib

    dbmod = importlib.import_module("shiftwise.db")
    return dbmod.database_engine() == "postgres"


def truncate_postgres_tables(conn):
    """Empty every application table and reset identity sequences.

    PostgreSQL has no per-test database file to swap (a shared DB ignores
    ``app.DB_PATH``), so tests must reset state instead. One CASCADE statement
    clears child tables regardless of foreign-key order.
    """
    conn.execute("TRUNCATE TABLE " + ", ".join(POSTGRES_TABLES) + " RESTART IDENTITY CASCADE")
    conn.commit()


def insert_id(conn, sql, params=()):
    """INSERT a row and return its auto-generated ``id`` on either backend.

    ``cursor.lastrowid`` does not exist on psycopg cursors, so tests must use
    ``RETURNING id`` instead. SQLite has supported ``RETURNING`` since 3.35.
    The statement must not end with a semicolon.
    """
    row = conn.execute(sql + " RETURNING id", params).fetchone()
    return row[0]


def isolate_database(appmod):
    temporary = tempfile.TemporaryDirectory(prefix="shiftwise-test-")
    appmod.DB_PATH = Path(temporary.name) / "scheduler.db"
    return temporary
