import sys
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


def truncate_postgres_tables(conn):
    """Empty all nine application tables and reset PostgreSQL identity sequences.

    One CASCADE statement clears child tables regardless of foreign-key order.
    """
    conn.execute("TRUNCATE TABLE " + ", ".join(POSTGRES_TABLES) + " RESTART IDENTITY CASCADE")
    conn.commit()


def insert_id(conn, sql, params=()):
    """INSERT a PostgreSQL row and return its generated ``id`` with ``RETURNING``.

    The statement must not end with a semicolon.
    """
    row = conn.execute(sql + " RETURNING id", params).fetchone()
    return row[0]
