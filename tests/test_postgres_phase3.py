"""Phase 3 tests: Live PostgreSQL route and engine query verification.

Validates that route queries, conflict handling (ON CONFLICT), temporal scalar
normalization, declarative cascades, transaction rollback, and scheduler
execution work against a real PostgreSQL instance.

When a live PostgreSQL server is reachable (via DATABASE_URL or the local dev
container), these tests execute against it. Otherwise, they skip gracefully.
"""

import os
from datetime import date, datetime, timedelta

import pytest

import app as appmod
import mock_seed
from shiftwise.db import (
    UNIQUE_VIOLATION_ERRORS,
    db,
    init_db,
    monday_of,
)
from shiftwise.domain.constants import DAYS
from shiftwise.scheduler.engine import run_scheduler
from shiftwise.security import use_csrf_aware_test_client

POSTGRES_TEST_URL = os.environ.get(
    "TEST_DATABASE_URL",
    os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:devpass@127.0.0.1:5432/shiftwise_phase3_test",
    ),
)


def _is_postgres_available():
    try:
        import psycopg

        with psycopg.connect(POSTGRES_TEST_URL, connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.skipif(
        not _is_postgres_available(),
        reason="Live PostgreSQL server not reachable at " + POSTGRES_TEST_URL,
    ),
]


@pytest.fixture(autouse=True)
def postgres_env(_postgres_test_isolation, monkeypatch):
    """Point application to the live test PostgreSQL database for each test."""
    monkeypatch.setenv("SHIFTWISE_DB_ENGINE", "postgres")
    monkeypatch.setenv("DATABASE_URL", POSTGRES_TEST_URL)
    monkeypatch.setenv("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD", "bootstrap-manager-pass")

    # Initialize a clean schema before each test
    init_db(seed_demo=False)
    conn = db()
    for table in (
        "scheduler_rebuilds",
        "picks",
        "coverage_preferences",
        "assignments",
        "notifications",
        "requests",
        "login_attempts",
        "shifts",
        "users",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()

    yield

    conn = db()
    for table in (
        "scheduler_rebuilds",
        "picks",
        "coverage_preferences",
        "assignments",
        "notifications",
        "requests",
        "login_attempts",
        "shifts",
        "users",
    ):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    conn.close()


def test_postgres_pool_guc_options():
    """Verify ConnectionPool configures statement_timeout and lock_timeout via options."""
    conn = db()
    cur = conn.cursor()
    cur.execute("SHOW statement_timeout;")
    st = cur.fetchone()[0]
    cur.execute("SHOW lock_timeout;")
    lt = cur.fetchone()[0]
    conn.close()
    assert st == "30s"
    assert lt == "5s"


def test_postgres_temporal_normalization():
    """PostgreSQL temporal columns use the string shapes consumed by the application."""
    conn = db()
    today_str = date.today().isoformat()
    now_str = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO users (username, password, name, role, hired_on) "
        "VALUES (?, ?, ?, 'employee', ?)",
        ("worker1", "pass123", "Worker One", today_str),
    )
    user_id = conn.execute("SELECT id, hired_on FROM users WHERE username='worker1'").fetchone()
    assert isinstance(user_id["hired_on"], str)
    assert user_id["hired_on"] == today_str

    conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
        "VALUES (?, 'Mon', '09:00', '17:00', 1, 'front')",
        (monday_of(date.today()).isoformat(),),
    )
    shift = conn.execute("SELECT start_time, end_time FROM shifts LIMIT 1").fetchone()
    assert isinstance(shift["start_time"], str)
    assert shift["start_time"] == "09:00"
    assert isinstance(shift["end_time"], str)
    assert shift["end_time"] == "17:00"

    conn.execute(
        "INSERT INTO notifications (user_id, kind, message, created_at) VALUES (?, ?, ?, ?)",
        (user_id["id"], "alert", "Test message", now_str),
    )
    notif = conn.execute("SELECT created_at FROM notifications LIMIT 1").fetchone()
    assert isinstance(notif["created_at"], str)
    assert "T" in notif["created_at"]
    conn.commit()
    conn.close()


def test_postgres_mock_seed_and_counts():
    """Verify full demo mock seeding on live PostgreSQL."""
    n_emp, n_days, n_picks = mock_seed.seed(appmod, force=True)
    assert n_emp == 14
    assert n_days == 7
    assert n_picks == 588

    conn = db()
    emp_count = conn.execute("SELECT COUNT(*) c FROM users WHERE role='employee'").fetchone()["c"]
    shift_count = conn.execute("SELECT COUNT(*) c FROM shifts").fetchone()["c"]
    pick_count = conn.execute("SELECT COUNT(*) c FROM picks").fetchone()["c"]
    conn.close()
    assert emp_count == 14
    assert shift_count == 84  # 2 weeks * 7 days * 3 windows * 2 houses
    assert pick_count == 588


def test_postgres_on_conflict_picks_upsert():
    """Verify ON CONFLICT (user_id, shift_id) DO UPDATE SET rank=excluded.rank preserves row ID."""
    mock_seed.seed(appmod, force=True)
    conn = db()
    maria = conn.execute("SELECT id FROM users WHERE username='maria'").fetchone()["id"]
    shift = conn.execute("SELECT id FROM shifts LIMIT 1").fetchone()["id"]

    conn.execute(
        "INSERT INTO picks (user_id, shift_id, rank) VALUES (?, ?, 1) "
        "ON CONFLICT (user_id, shift_id) DO UPDATE SET rank=excluded.rank",
        (maria, shift),
    )
    conn.commit()
    pick_id1 = conn.execute(
        "SELECT id, rank FROM picks WHERE user_id=? AND shift_id=?", (maria, shift)
    ).fetchone()
    assert pick_id1["rank"] == 1

    # Update rank for same (user_id, shift_id)
    conn.execute(
        "INSERT INTO picks (user_id, shift_id, rank) VALUES (?, ?, 5) "
        "ON CONFLICT (user_id, shift_id) DO UPDATE SET rank=excluded.rank",
        (maria, shift),
    )
    conn.commit()
    pick_id2 = conn.execute(
        "SELECT id, rank FROM picks WHERE user_id=? AND shift_id=?", (maria, shift)
    ).fetchone()
    assert pick_id2["rank"] == 5
    assert pick_id2["id"] == pick_id1["id"]  # Row ID does not churn
    conn.close()


def test_postgres_on_conflict_assignments_do_nothing():
    """Verify assignments ON CONFLICT DO NOTHING ignores duplicates cleanly."""
    mock_seed.seed(appmod, force=True)
    conn = db()
    maria = conn.execute("SELECT id FROM users WHERE username='maria'").fetchone()["id"]
    shift = conn.execute("SELECT id FROM shifts LIMIT 1").fetchone()["id"]

    conn.execute(
        "INSERT INTO assignments (shift_id, user_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (shift, maria),
    )
    conn.commit()
    count1 = conn.execute(
        "SELECT COUNT(*) c FROM assignments WHERE shift_id=? AND user_id=?", (shift, maria)
    ).fetchone()["c"]
    assert count1 == 1

    # Duplicate insert does nothing
    conn.execute(
        "INSERT INTO assignments (shift_id, user_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
        (shift, maria),
    )
    conn.commit()
    count2 = conn.execute(
        "SELECT COUNT(*) c FROM assignments WHERE shift_id=? AND user_id=?", (shift, maria)
    ).fetchone()["c"]
    assert count2 == 1
    conn.close()


def test_postgres_on_conflict_coverage_preferences():
    """Verify coverage_preferences upsert sets boolean willing flag correctly."""
    mock_seed.seed(appmod, force=True)
    conn = db()
    maria = conn.execute("SELECT id FROM users WHERE username='maria'").fetchone()["id"]
    shift = conn.execute("SELECT id FROM shifts LIMIT 1").fetchone()["id"]

    conn.execute(
        "INSERT INTO coverage_preferences (user_id, shift_id, willing) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, shift_id) DO UPDATE SET willing=excluded.willing",
        (maria, shift, True),
    )
    conn.commit()
    pref1 = conn.execute(
        "SELECT willing FROM coverage_preferences WHERE user_id=? AND shift_id=?",
        (maria, shift),
    ).fetchone()["willing"]
    assert pref1 is True

    conn.execute(
        "INSERT INTO coverage_preferences (user_id, shift_id, willing) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, shift_id) DO UPDATE SET willing=excluded.willing",
        (maria, shift, False),
    )
    conn.commit()
    pref2 = conn.execute(
        "SELECT willing FROM coverage_preferences WHERE user_id=? AND shift_id=?",
        (maria, shift),
    ).fetchone()["willing"]
    assert pref2 is False
    conn.close()


def test_postgres_unique_violation_catch():
    """Verify UNIQUE_VIOLATION_ERRORS catches duplicate key on live PostgreSQL."""
    conn = db()
    conn.execute("INSERT INTO users (username, password, name) VALUES ('dupe', 'hash', 'Dupe 1')")
    conn.commit()

    with pytest.raises(UNIQUE_VIOLATION_ERRORS):
        conn.execute(
            "INSERT INTO users (username, password, name) VALUES ('dupe', 'hash2', 'Dupe 2')"
        )
        conn.commit()
    conn.close()


def test_postgres_close_rolls_back_uncommitted_transaction():
    """Closing a lease must roll back writes before returning it to the pool."""
    conn = db()
    conn.execute(
        "INSERT INTO users (username, password, name) VALUES ('uncommitted', 'hash', 'Uncommitted')"
    )
    # Close without commit -> must roll back
    conn.close()

    conn2 = db()
    row = conn2.execute("SELECT 1 FROM users WHERE username='uncommitted'").fetchone()
    conn2.close()
    assert row is None


def test_postgres_catalog_tables_and_fk_delete_rules():
    """The live schema must expose all application tables and exact FK actions."""
    expected_tables = {
        "users",
        "shifts",
        "picks",
        "coverage_preferences",
        "assignments",
        "requests",
        "notifications",
        "login_attempts",
        "scheduler_rebuilds",
    }
    expected_rules = {
        ("picks", "user_id"): "CASCADE",
        ("picks", "shift_id"): "CASCADE",
        ("coverage_preferences", "user_id"): "CASCADE",
        ("coverage_preferences", "shift_id"): "CASCADE",
        ("assignments", "shift_id"): "CASCADE",
        ("assignments", "user_id"): "CASCADE",
        ("requests", "user_id"): "CASCADE",
        ("notifications", "user_id"): "CASCADE",
        ("requests", "shift_id"): "SET NULL",
        ("requests", "target_shift_id"): "SET NULL",
        ("requests", "target_user_id"): "SET NULL",
        ("notifications", "shift_id"): "SET NULL",
    }
    conn = db()
    try:
        tables = {
            row["table_name"]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
            )
        }
        assert expected_tables <= tables
        rules = conn.execute(
            "SELECT kcu.table_name, kcu.column_name, rc.delete_rule "
            "FROM information_schema.referential_constraints rc "
            "JOIN information_schema.key_column_usage kcu "
            "ON kcu.constraint_catalog=rc.constraint_catalog "
            "AND kcu.constraint_schema=rc.constraint_schema "
            "AND kcu.constraint_name=rc.constraint_name "
            "WHERE kcu.table_schema=current_schema()"
        ).fetchall()
        assert sorted(tuple(row) for row in rules) == sorted(
            (table, column, action) for (table, column), action in expected_rules.items()
        )
    finally:
        conn.close()


def test_postgres_declarative_fk_cascades():
    """Shift deletion cascades staffing but preserves superseded history."""
    mock_seed.seed(appmod, force=True)
    conn = db()
    try:
        shift = conn.execute("SELECT id, week_start FROM shifts ORDER BY id LIMIT 1").fetchone()
        shift_id = shift["id"]
        maria = conn.execute("SELECT id FROM users WHERE username='maria'").fetchone()["id"]
        devon = conn.execute("SELECT id FROM users WHERE username='devon'").fetchone()["id"]
        future_week = (date.fromisoformat(shift["week_start"]) + timedelta(weeks=1)).isoformat()
        other_shift = conn.execute(
            "INSERT INTO shifts (week_start, day, start_time, end_time, area) "
            "VALUES (?, 'Mon', '09:00', '17:00', 'front') RETURNING id",
            (future_week,),
        ).fetchone()["id"]
        conn.executemany(
            "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'manager_fixed')",
            [(shift_id, maria), (other_shift, devon)],
        )
        conn.executemany(
            "INSERT INTO coverage_preferences (user_id, shift_id, willing) VALUES (?, ?, ?)",
            [(maria, shift_id, True), (devon, other_shift, False)],
        )
        conn.execute(
            "INSERT INTO picks (user_id, shift_id, rank) VALUES (?, ?, 1)", (devon, other_shift)
        )
        source_request = conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id, target_shift_id) "
            "VALUES (?, 'switch', ?, ?) RETURNING id",
            (maria, shift_id, other_shift),
        ).fetchone()["id"]
        target_request = conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id, target_shift_id) "
            "VALUES (?, 'switch', ?, ?) RETURNING id",
            (devon, other_shift, shift_id),
        ).fetchone()["id"]
        other_request = conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id, status) "
            "VALUES (?, 'sick', ?, 'denied') RETURNING id",
            (devon, other_shift),
        ).fetchone()["id"]
        history_notification = conn.execute(
            "INSERT INTO notifications (user_id, shift_id, kind, message) "
            "VALUES (?, ?, 'conflict', 'Archived shift history') RETURNING id",
            (maria, shift_id),
        ).fetchone()["id"]
        other_notification = conn.execute(
            "INSERT INTO notifications (user_id, shift_id, kind, message, read) "
            "VALUES (?, ?, 'conflict', 'Unaffected shift history', TRUE) RETURNING id",
            (devon, other_shift),
        ).fetchone()["id"]
        conn.commit()
        for table in ("picks", "coverage_preferences", "assignments"):
            assert conn.execute(f"SELECT 1 FROM {table} WHERE shift_id=?", (shift_id,)).fetchone()
    finally:
        conn.close()

    client = appmod.app.test_client()
    assert (
        client.post("/login", data={"username": "manager", "password": "manager"}).status_code
        == 302
    )
    response = client.post(f"/manager/shift/delete/{shift_id}")
    assert response.status_code == 302

    conn = db()
    try:
        assert conn.execute("SELECT id FROM shifts WHERE id=?", (shift_id,)).fetchone() is None
        for table in ("picks", "coverage_preferences", "assignments"):
            assert (
                conn.execute(f"SELECT 1 FROM {table} WHERE shift_id=?", (shift_id,)).fetchall()
                == []
            )
        for request_id, expected_source, expected_target in (
            (source_request, None, other_shift),
            (target_request, other_shift, None),
        ):
            history = conn.execute(
                "SELECT shift_id, target_shift_id, status FROM requests WHERE id=?", (request_id,)
            ).fetchone()
            assert tuple(history) == (expected_source, expected_target, "superseded")
        history = conn.execute(
            "SELECT user_id, shift_id, message FROM notifications WHERE id=?",
            (history_notification,),
        ).fetchone()
        assert tuple(history) == (maria, None, "Archived shift history")
        assert conn.execute("SELECT id FROM users WHERE id=?", (devon,)).fetchone()["id"] == devon
        assert conn.execute("SELECT id FROM shifts WHERE id=?", (other_shift,)).fetchone()[
            "id"
        ] == (other_shift)
        assert tuple(
            conn.execute(
                "SELECT user_id, rank FROM picks WHERE shift_id=?", (other_shift,)
            ).fetchone()
        ) == (devon, 1)
        assert tuple(
            conn.execute(
                "SELECT user_id, willing FROM coverage_preferences WHERE shift_id=?", (other_shift,)
            ).fetchone()
        ) == (devon, False)
        assert tuple(
            conn.execute(
                "SELECT user_id, status FROM assignments WHERE shift_id=?", (other_shift,)
            ).fetchone()
        ) == (devon, "manager_fixed")
        assert tuple(
            conn.execute(
                "SELECT user_id, shift_id, status FROM requests WHERE id=?", (other_request,)
            ).fetchone()
        ) == (devon, other_shift, "denied")
        assert tuple(
            conn.execute(
                "SELECT user_id, shift_id, message, read FROM notifications WHERE id=?",
                (other_notification,),
            ).fetchone()
        ) == (devon, other_shift, "Unaffected shift history", True)
    finally:
        conn.close()


def test_postgres_user_cascades_preserve_other_users_and_shifts():
    """Account deletion cascades owned rows, nulls invitee history, and stays scoped."""
    mock_seed.seed(appmod, force=True)
    conn = db()
    try:
        maria = conn.execute("SELECT id FROM users WHERE username='maria'").fetchone()["id"]
        devon = conn.execute("SELECT id FROM users WHERE username='devon'").fetchone()["id"]
        shift_id, other_shift = [
            row["id"] for row in conn.execute("SELECT id FROM shifts ORDER BY id LIMIT 2")
        ]
        other_pick = tuple(
            conn.execute(
                "SELECT user_id, shift_id, rank FROM picks WHERE user_id=? AND shift_id=?",
                (devon, other_shift),
            ).fetchone()
        )
        conn.executemany(
            "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'manager_fixed')",
            [(shift_id, maria), (other_shift, devon)],
        )
        conn.executemany(
            "INSERT INTO coverage_preferences (user_id, shift_id, willing) VALUES (?, ?, ?)",
            [(maria, shift_id, True), (maria, other_shift, True), (devon, other_shift, False)],
        )
        conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id) VALUES (?, 'sick', ?)",
            (maria, shift_id),
        )
        invite_history = conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id, target_user_id, status) "
            "VALUES (?, 'swap', ?, ?, 'superseded') RETURNING id",
            (devon, other_shift, maria),
        ).fetchone()["id"]
        other_request = conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id) VALUES (?, 'sick', ?) RETURNING id",
            (devon, other_shift),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO notifications (user_id, shift_id, kind, message) "
            "VALUES (?, ?, 'conflict', 'Deleted account history')",
            (maria, shift_id),
        )
        other_notification = conn.execute(
            "INSERT INTO notifications (user_id, shift_id, kind, message, read) "
            "VALUES (?, ?, 'conflict', 'Unaffected account history', TRUE) RETURNING id",
            (devon, other_shift),
        ).fetchone()["id"]
        conn.commit()
        for table in ("picks", "coverage_preferences", "assignments", "requests", "notifications"):
            assert conn.execute(f"SELECT 1 FROM {table} WHERE user_id=?", (maria,)).fetchone()

        conn.execute("DELETE FROM users WHERE id=?", (maria,))
        conn.commit()

        assert conn.execute("SELECT id FROM users WHERE id=?", (maria,)).fetchone() is None
        for table in ("picks", "coverage_preferences", "assignments", "requests", "notifications"):
            assert conn.execute(f"SELECT 1 FROM {table} WHERE user_id=?", (maria,)).fetchall() == []
        assert conn.execute("SELECT id FROM users WHERE id=?", (devon,)).fetchone()["id"] == devon
        assert [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM shifts WHERE id IN (?, ?) ORDER BY id", (shift_id, other_shift)
            )
        ] == [shift_id, other_shift]
        assert tuple(
            conn.execute(
                "SELECT shift_id, willing FROM coverage_preferences WHERE user_id=?", (devon,)
            ).fetchone()
        ) == (other_shift, False)
        assert tuple(
            conn.execute(
                "SELECT user_id, status FROM assignments WHERE shift_id=?", (other_shift,)
            ).fetchone()
        ) == (devon, "manager_fixed")
        assert (
            tuple(
                conn.execute(
                    "SELECT user_id, shift_id, rank FROM picks WHERE user_id=? AND shift_id=?",
                    (devon, other_shift),
                ).fetchone()
            )
            == other_pick
        )
        assert tuple(
            conn.execute(
                "SELECT user_id, shift_id, target_user_id, status FROM requests WHERE id=?",
                (invite_history,),
            ).fetchone()
        ) == (devon, other_shift, None, "superseded")
        assert tuple(
            conn.execute(
                "SELECT user_id, shift_id, status FROM requests WHERE id=?", (other_request,)
            ).fetchone()
        ) == (devon, other_shift, "approved")
        assert tuple(
            conn.execute(
                "SELECT user_id, shift_id, message, read FROM notifications WHERE id=?",
                (other_notification,),
            ).fetchone()
        ) == (devon, other_shift, "Unaffected account history", True)
    finally:
        conn.close()


def test_postgres_roster_ordering_query():
    """Verify boolean expression ordering in roster query works on PostgreSQL."""
    conn = db()
    conn.execute(
        "INSERT INTO users (username, password, name, role, employment_type, hired_on) VALUES "
        "('pt_late', 'p', 'PT Late', 'employee', 'part_time', '2024-01-01'), "
        "('ft_new', 'p', 'FT New', 'employee', 'full_time', '2023-01-01'), "
        "('ft_old', 'p', 'FT Old', 'employee', 'full_time', '2020-01-01'), "
        "('pt_early', 'p', 'PT Early', 'employee', 'part_time', '2022-01-01')"
    )
    conn.commit()

    rows = conn.execute(
        "SELECT username FROM users WHERE role='employee' "
        "ORDER BY employment_type='full_time' DESC, hired_on ISNULL, hired_on"
    ).fetchall()
    conn.close()
    usernames = [r["username"] for r in rows]
    # Full time first (oldest first), then part time (oldest first)
    assert usernames == ["ft_old", "ft_new", "pt_early", "pt_late"]


def test_postgres_scheduler_and_routes_flow():
    """Test full HTTP flow through Flask test client running on live PostgreSQL."""
    mock_seed.seed(appmod, force=True)
    use_csrf_aware_test_client(appmod.app)
    client = appmod.app.test_client()

    # 1. Login as employee
    r = client.post(
        "/login", data={"username": "maria", "password": "maria"}, follow_redirects=True
    )
    assert b"Log out" in r.data

    # 2. View dashboard without changing notification state
    dash = client.get("/")
    assert dash.status_code == 200

    # 3. Submit day picks
    form = {f"rank_day_{day}": str(i + 1) for i, day in enumerate(DAYS)}
    pick_resp = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved" in pick_resp.data

    # 4. Request a day off
    off_resp = client.post("/request/day_off", data={"day": "Wed"}, follow_redirects=True)
    assert b"Day off requested" in off_resp.data

    # 5. Run scheduler over week
    week = monday_of(date.today()).isoformat()
    n_picks = run_scheduler(week)
    assert n_picks > 0

    # 6. Verify calendar healthz check
    healthz = client.get("/healthz")
    assert healthz.status_code == 200
    assert healthz.json == {"status": "ok"}
