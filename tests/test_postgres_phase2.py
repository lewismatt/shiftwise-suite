"""PostgreSQL schema parsing, initialization, transaction, and route regressions.

Live server regressions are in test_postgres_phase3.py and test_postgres_phase4.py.
"""

import importlib

import pytest
from flask import session

import app as appmod

sdb = importlib.import_module("shiftwise.db")
mock_seed = importlib.import_module("mock_seed")
manager_routes = importlib.import_module("shiftwise.routes.manager")
roster_routes = importlib.import_module("shiftwise.routes.roster")
auth_mod = importlib.import_module("shiftwise.auth")


# ---------------------------------------------------------------------------
# DDL structure
# ---------------------------------------------------------------------------


def test_postgres_ddl_parses_as_valid_postgres():
    pglast = pytest.importorskip("pglast")
    for statement in sdb._split_ddl(sdb.POSTGRES_SCHEMA):
        pglast.parse_sql(statement)  # raises on syntax error


def test_split_ddl_ignores_semicolons_in_strings_comments_and_dollar_quotes():
    ddl = (
        "CREATE TABLE t (note TEXT DEFAULT 'a;b');\n"
        "-- a line comment with ; semicolon\n"
        "CREATE TABLE u (id INT); /* block ; comment */\n"
        'CREATE TABLE v ("weird;name" INT);\n'
        "CREATE FUNCTION f() RETURNS void AS $$\n"
        "BEGIN RAISE NOTICE 'x;y'; END;\n"
        "$$ LANGUAGE plpgsql;"
    )
    statements = sdb._split_ddl(ddl)
    assert len(statements) == 4
    assert statements[0] == "CREATE TABLE t (note TEXT DEFAULT 'a;b')"
    assert statements[1].endswith("CREATE TABLE u (id INT)")
    # Comments attach to the statement they precede; the quoted identifier
    # keeps its semicolon.
    assert statements[2].endswith('CREATE TABLE v ("weird;name" INT)')
    assert "/* block ; comment */" in statements[2]
    assert statements[3].startswith("CREATE FUNCTION f()")
    assert "RAISE NOTICE 'x;y'" in statements[3]
    assert statements[3].endswith("$$ LANGUAGE plpgsql")


def test_split_ddl_handles_tagged_dollar_quotes_and_escapes():
    ddl = (
        "CREATE FUNCTION g() RETURNS void AS $body$\n"
        "BEGIN RAISE NOTICE 'it''s; here'; END;\n"
        "$body$ LANGUAGE plpgsql;\n"
        "CREATE TABLE w (note TEXT DEFAULT 'don''t; split');"
    )
    statements = sdb._split_ddl(ddl)
    assert len(statements) == 2
    assert "RAISE NOTICE 'it''s; here'" in statements[0]
    assert statements[1] == "CREATE TABLE w (note TEXT DEFAULT 'don''t; split')"


# ---------------------------------------------------------------------------
# Recording fake connection
# ---------------------------------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __iter__(self):
        return iter(self._rows)


class _RecordingConn:
    """Stands in for a DB connection: records statements, answers canned reads."""

    def __init__(self, handler=None):
        self.statements = []  # list of (sql, params)
        self.executemany_calls = []  # list of (sql, [params, ...])
        self.events = []  # ordered event log ("execute", "commit", ...)
        self.committed = False
        self.closed = False
        self._handler = handler or (lambda sql, params: [])

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        self.events.append("execute")
        return _Rows(self._handler(sql, params))

    def executemany(self, sql, seq):
        seq = list(seq)
        self.executemany_calls.append((sql, seq))
        self.events.append("executemany")
        return None

    def commit(self):
        self.events.append("commit")
        self.committed = True

    def rollback(self):
        self.events.append("rollback")

    def close(self):
        self.events.append("close")
        self.closed = True


def _postgres_env(monkeypatch):
    monkeypatch.setenv("SHIFTWISE_DB_ENGINE", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")


# ---------------------------------------------------------------------------
# init_db() per-engine dispatch
# ---------------------------------------------------------------------------


def test_init_db_postgres_rejects_short_bootstrap_password(monkeypatch):
    _postgres_env(monkeypatch)
    monkeypatch.setenv("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD", "short")
    conn = _RecordingConn()
    monkeypatch.setattr(sdb, "db", lambda: conn)
    with pytest.raises(RuntimeError, match="SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD"):
        sdb.init_db()


def test_init_db_postgres_commits_schema_before_mock_seed_handoff(monkeypatch):
    """Regression test for the review's Blocker 1.

    _seed_if_empty closes the init lease and mock_seed.seed() opens its own
    pooled connection; the DDL commit must land before that handoff or the
    pool rolls the uncommitted schema back (UndefinedTable on the first
    DELETE FROM). The recording facade is faithful here: commit/close are
    real ordered events, not no-ops.
    """
    _postgres_env(monkeypatch)
    conn = _RecordingConn()
    monkeypatch.setattr(sdb, "db", lambda: conn)
    seed_calls = []
    monkeypatch.setattr(
        mock_seed,
        "seed",
        lambda appmod, force=False: (
            conn.events.append("mock_seed.seed"),
            seed_calls.append(force),
        ),
    )

    sdb.init_db(mock_roster=True)

    # The schema commit lands before mock_seed opens its own connection.
    assert "mock_seed.seed" in conn.events
    assert conn.events.index("commit") < conn.events.index("mock_seed.seed")
    # The demo-seed handoff is an intended wipe: force=True must be passed.
    assert seed_calls == [True]


def test_init_db_postgres_rolls_back_and_returns_lease_on_ddl_failure(monkeypatch):
    """init_db must not leak the pooled lease on failure."""
    _postgres_env(monkeypatch)

    class _FailingConn(_RecordingConn):
        def execute(self, sql, params=()):
            if sql.startswith("CREATE TABLE"):
                raise RuntimeError("simulated DDL failure")
            return super().execute(sql, params)

    conn = _FailingConn()
    monkeypatch.setattr(sdb, "db", lambda: conn)
    with pytest.raises(RuntimeError, match="simulated DDL failure"):
        sdb.init_db()
    assert "rollback" in conn.events
    assert conn.closed


# ---------------------------------------------------------------------------
# Delete routes: preserve history ordering and rely on database cascades
# ---------------------------------------------------------------------------


def _auth_handler(sql, params):
    if sql.startswith("SELECT * FROM users WHERE id="):
        return [{"id": 1, "role": "manager"}]
    return []


def _login_and_call(monkeypatch, routes_mod, route_fn, path, form=None, handler=None):
    conn = _RecordingConn(handler=handler or _auth_handler)
    monkeypatch.setattr(routes_mod, "db", lambda: conn)
    monkeypatch.setattr(auth_mod, "db", lambda: conn)
    monkeypatch.setattr(routes_mod, "run_scheduler", lambda week: None)
    with appmod.app.test_request_context(path, method="POST", data=form or {}):
        session["uid"] = 1
        session["role"] = "manager"
        response = route_fn()
    return conn, response


def test_delete_shift_postgres_relies_on_db_cascade(monkeypatch):
    def handler(sql, params):
        if sql.startswith("SELECT week_start FROM shifts"):
            return [{"week_start": "2026-09-28"}]
        return _auth_handler(sql, params)

    conn, response = _login_and_call(
        monkeypatch,
        manager_routes,
        lambda: manager_routes.delete_shift(9),
        "/manager/shift/delete/9",
        handler=handler,
    )
    sqls = [sql for sql, _ in conn.statements]
    supersede = next(
        index for index, sql in enumerate(sqls) if sql.startswith("UPDATE requests SET status=")
    )
    delete = next(
        index for index, sql in enumerate(sqls) if sql.startswith("DELETE FROM shifts WHERE id=?")
    )
    assert "status='superseded'" in sqls[supersede]
    assert conn.statements[supersede][1] == (9, 9)
    assert conn.statements[delete][1] == (9,)
    assert supersede < delete
    for table in ("picks", "coverage_preferences", "assignments", "requests", "notifications"):
        assert not any(sql.startswith(f"DELETE FROM {table} ") for sql in sqls), table
    assert response.status_code == 302
    assert conn.committed and conn.closed


def test_delete_employee_postgres_relies_on_db_cascade(monkeypatch):
    def handler(sql, params):
        if sql.startswith("SELECT id FROM users WHERE id=?"):
            return [{"id": 5}]
        return _auth_handler(sql, params)

    conn, response = _login_and_call(
        monkeypatch,
        roster_routes,
        lambda: roster_routes.delete_employee(5),
        "/manager/roster/5/delete",
        form={"confirm": "yes"},
        handler=handler,
    )
    sqls = [sql for sql, _ in conn.statements]
    for table in ("assignments", "picks", "coverage_preferences", "requests", "notifications"):
        assert not any(sql.startswith(f"DELETE FROM {table} WHERE user_id=?") for sql in sqls)
    cancel = sqls.index("DELETE FROM requests WHERE target_user_id=?")
    delete = sqls.index("DELETE FROM users WHERE id=? AND role='employee'")
    assert cancel < delete
    assert conn.statements[cancel][1] == (5,)
    assert conn.statements[delete][1] == (5,)
    assert response.status_code == 302
    assert conn.committed and conn.closed
