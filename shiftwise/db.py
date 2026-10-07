"""ShiftWise database connection, schema, and lifecycle management."""

import atexit
import os
import re
import sys
import threading
import zlib
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from urllib.parse import quote

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict
from psycopg.errors import UniqueViolation
from werkzeug.security import generate_password_hash

# Declarative foreign keys cascade dependent rows and preserve optional
# request/notification history with ON DELETE SET NULL.
POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'employee',
    weekly_hours INTEGER DEFAULT 40,
    employment_type TEXT NOT NULL DEFAULT 'part_time',
    hired_on DATE,
    station TEXT NOT NULL DEFAULT 'front',
    email TEXT,
    phone TEXT,
    time_format TEXT NOT NULL DEFAULT '24h'
);
CREATE TABLE IF NOT EXISTS shifts (
    id BIGSERIAL PRIMARY KEY,
    week_start DATE NOT NULL,
    day TEXT NOT NULL,
    start_time TIME NOT NULL,
    end_time TIME NOT NULL,
    slots INTEGER NOT NULL DEFAULT 1 CHECK (slots >= 1),
    note TEXT,
    area TEXT NOT NULL DEFAULT 'front'
);
CREATE TABLE IF NOT EXISTS picks (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    shift_id BIGINT NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
    rank INTEGER NOT NULL,
    UNIQUE(user_id, shift_id)
);
CREATE TABLE IF NOT EXISTS coverage_preferences (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    shift_id BIGINT NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
    willing BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY(user_id, shift_id)
);
CREATE TABLE IF NOT EXISTS assignments (
    id BIGSERIAL PRIMARY KEY,
    shift_id BIGINT NOT NULL REFERENCES shifts(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'proposed',
    UNIQUE(shift_id, user_id)
);
CREATE TABLE IF NOT EXISTS requests (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    shift_id BIGINT REFERENCES shifts(id) ON DELETE SET NULL,
    day TEXT,
    target_shift_id BIGINT REFERENCES shifts(id) ON DELETE SET NULL,
    vacation_start DATE,
    vacation_end DATE,
    week_start DATE,
    target_user_id BIGINT REFERENCES users(id) ON DELETE SET NULL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'approved',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS notifications (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    shift_id BIGINT REFERENCES shifts(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    read BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS login_attempts (
    key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    locked_until TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS scheduler_rebuilds (
    week_start DATE PRIMARY KEY,
    due_at TIMESTAMPTZ NOT NULL,
    generation BIGINT NOT NULL DEFAULT 1
);
"""


def _split_ddl(schema):
    """Split a multi-statement DDL script into individual statements.

    psycopg executes one statement per call, so the CREATE TABLE block is
    split on statement terminators before being applied one at a time.
    Semicolons inside single-quoted strings (with '' escapes), double-quoted
    identifiers (with "" escapes), -- line comments, /* block comments, and
    dollar-quoted bodies ($$ or $tag$) do not terminate statements.
    """
    statements = []
    current = []
    index = 0
    length = len(schema)
    state = "normal"
    dollar_tag = ""
    while index < length:
        char = schema[index]
        ahead = schema[index + 1] if index + 1 < length else ""
        if state == "normal":
            if char == "'":
                state = "single"
                current.append(char)
            elif char == '"':
                state = "double"
                current.append(char)
            elif char == "-" and ahead == "-":
                state = "line_comment"
                current.extend((char, ahead))
                index += 1
            elif char == "/" and ahead == "*":
                state = "block_comment"
                current.extend((char, ahead))
                index += 1
            elif char == "$":
                tag_match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", schema[index:])
                if tag_match:
                    dollar_tag = tag_match.group(0)
                    state = "dollar"
                    current.append(dollar_tag)
                    index += len(dollar_tag) - 1
                else:
                    current.append(char)
            elif char == ";":
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
            else:
                current.append(char)
        elif state == "single":
            current.append(char)
            if char == "'":
                if ahead == "'":
                    current.append(ahead)
                    index += 1
                else:
                    state = "normal"
        elif state == "double":
            current.append(char)
            if char == '"':
                if ahead == '"':
                    current.append(ahead)
                    index += 1
                else:
                    state = "normal"
        elif state == "line_comment":
            current.append(char)
            if char == "\n":
                state = "normal"
        elif state == "block_comment":
            current.append(char)
            if char == "*" and ahead == "/":
                current.append(ahead)
                index += 1
                state = "normal"
        else:  # dollar-quoted body: only the matching tag ends it
            if schema.startswith(dollar_tag, index):
                current.append(dollar_tag)
                index += len(dollar_tag) - 1
                state = "normal"
            else:
                current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


_postgres_pool = None
_postgres_pool_url = None
_postgres_pool_lock = threading.Lock()


def _read_secret_file(path):
    """Read a Docker secret file, stripping trailing CR/LF characters.

    Secret files conventionally end with a newline that is not part of
    the secret. All trailing CR and LF characters are stripped. This matches
    what PostgreSQL actually uses at initialization: the image's file_env
    passes the raw bytes through, and initdb reads the first password-file
    line stripping trailing CR/LF — otherwise
    CRLF or multi-newline files produce different passwords in the app
    and the database. Raises RuntimeError when the file cannot be read
    or holds no usable value, so a mis-mounted secret fails fast at startup.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read().rstrip("\r\n")
    except OSError as exc:
        raise RuntimeError(f"POSTGRES_PASSWORD_FILE points at an unreadable file: {path}") from exc
    if not value:
        raise RuntimeError(f"POSTGRES_PASSWORD_FILE is empty: {path}")
    return value


def _resolve_database_url():
    """Return the PostgreSQL connection URL for this process.

    When POSTGRES_PASSWORD_FILE is set (the docker-compose.secrets.yml
    override), the password is read from that file and the URL is
    assembled from POSTGRES_USER / POSTGRES_DB, so the secret value never
    appears in the process environment. Otherwise DATABASE_URL is used
    verbatim.
    """
    password_file = os.environ.get("POSTGRES_PASSWORD_FILE")
    if password_file:
        password = _read_secret_file(password_file)
        user = os.environ.get("POSTGRES_USER", "shiftwise")
        database = os.environ.get("POSTGRES_DB", "shiftwise")
        return (
            "postgresql://"
            f"{quote(user, safe='')}:{quote(password, safe='')}"
            "@postgres:5432/"
            f"{quote(database, safe='')}"
        )
    return os.environ.get("DATABASE_URL", "").strip()


def _database_engine():
    """Require PostgreSQL connection information; never fall back to a local database."""
    configured = os.environ.get("SHIFTWISE_DB_ENGINE")
    if configured is not None:
        engine = configured.lower()
        if engine == "sqlite":
            raise RuntimeError("SQLite support removed — configure PostgreSQL via DATABASE_URL")
        if engine not in ("postgres", "postgresql"):
            raise RuntimeError("SHIFTWISE_DB_ENGINE must be postgres or postgresql")
    database_url = _resolve_database_url()
    if not database_url:
        raise RuntimeError(
            "PostgreSQL is required. Set DATABASE_URL to "
            "postgresql://USER:PASSWORD@HOST:5432/DATABASE or start the Docker Compose stack."
        )
    try:
        conninfo_to_dict(database_url)
    except ProgrammingError:
        raise RuntimeError(
            "DATABASE_URL must be a PostgreSQL URL or libpq connection string"
        ) from None
    return "postgres"


def database_engine():
    """Return 'postgres' after validating database configuration."""
    return _database_engine()


@lru_cache(maxsize=1024)
def _qmark_to_psycopg(query):
    """Translate qmark parameters to psycopg `%s` placeholders.

    Literal `%` characters are escaped as `%%` (in every state, including
    inside string literals and comments) because psycopg interprets `%`
    as a placeholder introducer. The `%s` sequences this function inserts
    are emitted directly and never re-processed.

    Known limitations: PostgreSQL dollar-quoted strings (`$$...$$`),
    `E'...'` backslash escapes, and the `?` / `?|` / `?&` JSON operators are
    not specially handled. The application's SQL uses none of these; revisit
    if that changes. Callers must not pass queries that already contain
    `%s`/`%%` sequences.
    """
    result = []
    index = 0
    state = "normal"
    while index < len(query):
        char = query[index]
        next_char = query[index + 1] if index + 1 < len(query) else ""
        if char == "%":
            # Escape before any state handling: a literal % is a placeholder
            # introducer to psycopg everywhere, including inside strings.
            result.append("%%")
        elif state == "normal":
            if char == "'":
                state = "single"
                result.append(char)
            elif char == '"':
                state = "double"
                result.append(char)
            elif char == "-" and next_char == "-":
                state = "line_comment"
                result.extend((char, next_char))
                index += 1
            elif char == "/" and next_char == "*":
                state = "block_comment"
                result.extend((char, next_char))
                index += 1
            elif char == "?":
                result.append("%s")
            else:
                result.append(char)
        elif state == "single":
            result.append(char)
            if char == "'":
                if next_char == "'":
                    result.append(next_char)
                    index += 1
                else:
                    state = "normal"
        elif state == "double":
            result.append(char)
            if char == '"':
                if next_char == '"':
                    result.append(next_char)
                    index += 1
                else:
                    state = "normal"
        elif state == "line_comment":
            result.append(char)
            if char == "\n":
                state = "normal"
        else:
            result.append(char)
            if char == "*" and next_char == "/":
                result.append(next_char)
                index += 1
                state = "normal"
        index += 1
    return "".join(result)


class _PostgresRow(dict):
    """A dict row with positional indexing and value iteration."""

    def __init__(self, values, description):
        self._values = tuple(values)
        names = tuple(
            column.name if hasattr(column, "name") else column[0] for column in description
        )
        super().__init__(zip(names, self._values))

    def __iter__(self):
        return iter(self._values)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        return super().__getitem__(key)


def _temporal_scalar(value):
    """Normalize PostgreSQL temporal columns for routes, rules, and templates.

    Datetimes use naive local ISO timestamps, dates use YYYY-MM-DD, and
    times use HH:MM. Non-temporal values pass through unchanged.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return value


class _PostgresCursor:
    """Cursor facade translating qmarks and adapting returned rows."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=()):
        self._cursor.execute(_qmark_to_psycopg(query), params)
        return self

    def executemany(self, query, params_seq):
        self._cursor.executemany(_qmark_to_psycopg(query), params_seq)
        return self

    def _row(self, values):
        if values is None:
            return None
        return _PostgresRow([_temporal_scalar(v) for v in values], self._cursor.description)

    def fetchone(self):
        return self._row(self._cursor.fetchone())

    def fetchmany(self, size=None):
        if size is None:
            values = self._cursor.fetchmany()
        else:
            values = self._cursor.fetchmany(size)
        return [self._row(row) for row in values]

    def fetchall(self):
        return [self._row(row) for row in self._cursor.fetchall()]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._cursor.__exit__(exc_type, exc_val, exc_tb)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _PostgresConnection:
    """Lease facade returning a physical connection to its pool on close."""

    def __init__(self, pool, connection):
        self._pool = pool
        self._connection = connection
        self._closed = False

    def _check_closed(self):
        if self._closed or self._connection is None:
            raise RuntimeError("Cannot operate on a closed database connection")

    def cursor(self, *args, **kwargs):
        self._check_closed()
        return _PostgresCursor(self._connection.cursor(*args, **kwargs))

    def execute(self, query, params=()):
        return self.cursor().execute(query, params)

    def executemany(self, query, params_seq):
        return self.cursor().executemany(query, params_seq)

    def commit(self):
        self._check_closed()
        return self._connection.commit()

    def rollback(self):
        self._check_closed()
        return self._connection.rollback()

    def close(self):
        if not self._closed:
            conn = self._connection
            self._connection = None
            try:
                if conn is not None:
                    # Never hand a mid-transaction connection back to the
                    # pool: a caller that raised mid-transaction would
                    # otherwise leave the slot `idle in transaction
                    # (aborted)`, holding locks until a timeout reaps it and
                    # eventually exhausting the pool. A rollback on an idle
                    # connection is a harmless no-op.
                    conn.rollback()
                    self._pool.putconn(conn)
            finally:
                # Mark closed even if returning to the pool failed; the lease
                # is detached either way and must not be reused.
                self._closed = True

    def __enter__(self):
        self._check_closed()
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._check_closed()
        return self._connection.__exit__(exc_type, exc_val, exc_tb)

    def __getattr__(self, name):
        self._check_closed()
        return getattr(self._connection, name)


def _postgres_pool_for(database_url):
    global _postgres_pool, _postgres_pool_url
    with _postgres_pool_lock:
        if _postgres_pool is not None and _postgres_pool_url != database_url:
            _postgres_pool.close()
            _postgres_pool = None
            _postgres_pool_url = None
        if _postgres_pool is None:
            from psycopg_pool import ConnectionPool

            pool = ConnectionPool(
                conninfo=database_url,
                min_size=4,
                max_size=20,
                open=False,
                # Defensive server-side timeouts: a hung or runaway query
                # must not hold a pooled worker (or its locks) forever. The
                # review caught `idle in transaction (aborted)` holders
                # blocking DDL behind a transactionid lock; these GUCs bound
                # that failure mode. Scheduler compute is CPU-bound in
                # Python, so a 30s statement budget and 5s lock-wait budget
                # are generous backstops, not tight limits.
                kwargs={
                    "options": conninfo_to_dict(database_url).get("options", "")
                    + " -c statement_timeout=30s -c lock_timeout=5s"
                },
            )
            try:
                pool.open(wait=True, timeout=5)
            except Exception:
                pool.close()
                raise RuntimeError(
                    "PostgreSQL is unavailable. Check DATABASE_URL, database credentials, "
                    "and that the PostgreSQL server is running and reachable."
                ) from None
            _postgres_pool = pool
            _postgres_pool_url = database_url
        return _postgres_pool


def _close_postgres_pool():
    global _postgres_pool, _postgres_pool_url
    with _postgres_pool_lock:
        if _postgres_pool is not None:
            _postgres_pool.close()
            _postgres_pool = None
            _postgres_pool_url = None


atexit.register(_close_postgres_pool)


def db():
    """Lease a PostgreSQL connection from the application pool."""
    _database_engine()
    pool = _postgres_pool_for(_resolve_database_url())
    return _PostgresConnection(pool, pool.getconn())


def execute_sql(conn, query, params=()):
    """Execute a query through the selected connection's backend adapter."""
    return conn.execute(query, params)


def read_queries(conn, queries):
    """Batch independent SELECTs into one PostgreSQL pipeline round trip."""
    context = conn.pipeline()
    cursors = []
    try:
        with context:
            for query, params in queries:
                cursors.append(conn.execute(query, params))
        return [cursor.fetchall() for cursor in cursors]
    finally:
        for cursor in cursors:
            cursor.close()


def try_scheduler_lock(conn, week_start):
    """Acquire a transaction lock for this week; False means another run owns it."""
    key = zlib.crc32(str(week_start).encode("utf-8"))
    if key >= 2**31:
        key -= 2**32
    return conn.execute("SELECT pg_try_advisory_xact_lock(1398230099, ?)", (key,)).fetchone()[0]


def lock_shifts(conn, shift_ids):
    """Lock shift rows in ID order before reading or changing their staffing.

    PostgreSQL capacity checks must run as a separate statement after this
    lock: a count in the locking SELECT can use a snapshot from before a wait.
    """
    ids = sorted({sid for sid in shift_ids if sid is not None})
    if ids:
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"SELECT id FROM shifts WHERE id IN ({placeholders}) ORDER BY id FOR UPDATE", ids
        ).fetchall()


def lock_users(conn, user_ids, *, skip_locked=False):
    """Protect employee hours checks after taking the affected shift locks."""
    ids = sorted({uid for uid in user_ids if uid is not None})
    if ids:
        placeholders = ",".join("?" for _ in ids)
        suffix = " SKIP LOCKED" if skip_locked else ""
        return {
            row[0]
            for row in conn.execute(
                f"SELECT id FROM users WHERE id IN ({placeholders}) ORDER BY id "
                "FOR NO KEY UPDATE" + suffix,
                ids,
            )
        }
    return set(ids)


UNIQUE_VIOLATION_ERRORS = (UniqueViolation,)


def monday_of(d):
    """Return the Monday preceding or matching the given date."""
    return d - timedelta(days=d.weekday())


def _ensure_passwords_hashed(conn, seed_demo):
    """Hash plain-text passwords; require a bootstrap password for the demo manager."""
    for user in execute_sql(conn, "SELECT id, username, password FROM users").fetchall():
        password = user[2]
        if password.startswith(("scrypt:", "pbkdf2:")):
            continue
        if user[1] == "manager" and password == "manager" and not seed_demo:
            password = os.environ.get("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD")
            if not password or len(password) < 12:
                conn.close()
                raise RuntimeError(
                    "Set SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD (at least 12 characters) "
                    "to replace the demo manager password"
                )
        execute_sql(
            conn,
            "UPDATE users SET password=? WHERE id=?",
            (generate_password_hash(password), user[0]),
        )


def _seed_if_empty(conn, seed_demo, mock_roster):
    """Seed bootstrap or demo data when the users table is empty.

    Returns True when mock_seed.seed() took over (the connection is already
    closed); otherwise the caller still has to commit and close.
    """
    if execute_sql(conn, "SELECT 1 FROM users LIMIT 1").fetchone():
        return False
    bootstrap_password = os.environ.get("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD")
    if (
        mock_roster
        or seed_demo in ("8", "mock", 8)
        or (seed_demo and os.environ.get("SHIFTWISE_DEMO_SEED") in ("1", "8", "mock"))
    ):
        conn.close()
        import mock_seed

        target_mod = sys.modules.get("app") or sys.modules[__name__]
        mock_seed.seed(target_mod, force=True)
        return True
    if not seed_demo and (not bootstrap_password or len(bootstrap_password) < 12):
        conn.close()
        raise RuntimeError(
            "Set SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD (at least 12 characters) "
            "to create the first manager"
        )
    if not seed_demo:
        execute_sql(
            conn,
            "INSERT INTO users (username, password, name, role, weekly_hours, "
            "employment_type, station) VALUES (?,?,?,?,?,?,?)",
            (
                "manager",
                generate_password_hash(bootstrap_password),
                "Store Manager",
                "manager",
                40,
                "full_time",
                "front",
            ),
        )
    else:
        conn.executemany(
            "INSERT INTO users (username, password, name, role, weekly_hours,"
            " employment_type, hired_on, station) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    "manager",
                    generate_password_hash("manager"),
                    "Store Manager",
                    "manager",
                    40,
                    "full_time",
                    "2020-01-15",
                    "front",
                ),
                (
                    "alex",
                    generate_password_hash("alex"),
                    "Alex Rivera",
                    "employee",
                    30,
                    "full_time",
                    "2021-03-01",
                    "front",
                ),
                (
                    "sam",
                    generate_password_hash("sam"),
                    "Sam Chen",
                    "employee",
                    25,
                    "part_time",
                    "2023-06-10",
                    "front",
                ),
                (
                    "taylor",
                    generate_password_hash("taylor"),
                    "Taylor Brooks",
                    "employee",
                    20,
                    "part_time",
                    "2025-02-11",
                    "front",
                ),
                (
                    "jordan",
                    generate_password_hash("jordan"),
                    "Jordan Diaz",
                    "employee",
                    35,
                    "part_time",
                    "2024-11-20",
                    "back",
                ),
                (
                    "casey",
                    generate_password_hash("casey"),
                    "Casey Boots",
                    "employee",
                    35,
                    "part_time",
                    "2023-05-01",
                    "back",
                ),
                (
                    "morgan",
                    generate_password_hash("morgan"),
                    "Morgan Vale",
                    "employee",
                    40,
                    "full_time",
                    "2022-08-15",
                    "back",
                ),
            ],
        )
        week = monday_of(date.today()).isoformat()
        demo = [
            (week, "Mon", "09:00", "17:00", 1, None, "front"),
            (week, "Tue", "09:00", "17:00", 1, None, "front"),
            (week, "Wed", "09:00", "17:00", 1, None, "front"),
            (week, "Thu", "09:00", "17:00", 1, None, "front"),
            (week, "Fri", "09:00", "17:00", 1, None, "front"),
            (week, "Sat", "10:00", "18:00", 2, "Weekend rush", "front"),
            (week, "Sun", "11:00", "16:00", 1, "Short day", "front"),
            (week, "Mon", "06:00", "14:00", 1, None, "back"),
            (week, "Tue", "06:00", "14:00", 1, None, "back"),
            (week, "Wed", "06:00", "14:00", 1, None, "back"),
            (week, "Thu", "06:00", "14:00", 1, None, "back"),
            (week, "Fri", "11:00", "20:00", 1, "Dinner prep + service", "back"),
            (week, "Sat", "10:00", "20:00", 1, "Weekend covers", "back"),
            (week, "Sun", "10:00", "16:00", 1, "Brunch", "back"),
        ]
        conn.executemany(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots, note, area) "
            "VALUES (?,?,?,?,?,?,?)",
            demo,
        )
    return False


def init_db(seed_demo=False, mock_roster=False):
    """Initialize the PostgreSQL schema, enforce passwords, and seed empty data."""
    conn = db()
    try:
        for statement in _split_ddl(POSTGRES_SCHEMA):
            execute_sql(conn, statement)
        # The schema must be committed before the mock-seed handoff below:
        # _seed_if_empty closes this lease and mock_seed.seed() opens its
        # own pooled connection, which would otherwise roll the uncommitted
        # DDL back (UndefinedTable on the first DELETE FROM).
        conn.commit()
        _ensure_passwords_hashed(conn, seed_demo)
        if _seed_if_empty(conn, seed_demo, mock_roster):
            # Mock path: _seed_if_empty closed this lease itself and
            # mock_seed.seed() took over on its own connection; nothing
            # left to commit here.
            return
        conn.commit()
    except Exception:
        # The mock-seed handoff closes the lease inside _seed_if_empty, so
        # only roll back while the lease is still open: a closed facade
        # raises on rollback and would mask the original error.
        try:
            conn.rollback()
        except RuntimeError:
            pass
        raise
    finally:
        # close() is idempotent, so this is safe on the mock path where
        # _seed_if_empty already returned the lease to the pool; without it
        # a DDL failure would leak the slot and abandon AccessExclusive
        # locks on the new tables.
        conn.close()
