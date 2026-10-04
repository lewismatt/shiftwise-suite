"""Concurrency regressions using independent transactions on live PostgreSQL."""

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

import app as appmod
from shiftwise.scheduler import debounce, engine

TEST_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(not TEST_URL, reason="Set TEST_DATABASE_URL for PostgreSQL tests"),
    pytest.mark.pg_own_isolation,
]


@pytest.fixture
def pg(monkeypatch):
    schema = "phase4_" + uuid.uuid4().hex
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    monkeypatch.setenv("SHIFTWISE_DB_ENGINE", "postgres")
    monkeypatch.setenv("DATABASE_URL", make_conninfo(TEST_URL, options=f"-c search_path={schema}"))
    monkeypatch.setenv("SHIFTWISE_BOOTSTRAP_MANAGER_PASSWORD", "phase4-manager-password")
    appmod.init_db()
    week = appmod.monday_of(date.today()).isoformat()
    conn = appmod.db()
    conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
        "VALUES (?, 'Mon', '09:00', '10:00', 1)",
        (week,),
    )
    sid = conn.execute("SELECT id FROM shifts").fetchone()[0]
    for index in range(20):
        conn.execute(
            "INSERT INTO users (username, name, password, station) VALUES (?, ?, 'unused', 'front')",
            (f"worker{index}", f"Worker {index}"),
        )
    users = [r[0] for r in conn.execute("SELECT id FROM users WHERE role='employee' ORDER BY id")]
    manager = conn.execute("SELECT id FROM users WHERE role='manager'").fetchone()[0]
    conn.commit()
    conn.close()
    yield week, sid, users, manager
    appmod.app.extensions["scheduler_worker"].stop()
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def client_for(uid, role="employee"):
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session.update(uid=uid, role=role, name="Phase 4 test")
    return client


def settle():
    conn = appmod.db()
    conn.execute("UPDATE scheduler_rebuilds SET due_at=clock_timestamp() - interval '1 second'")
    conn.commit()
    conn.close()


def pending():
    conn = appmod.db()
    try:
        return conn.execute("SELECT generation FROM scheduler_rebuilds").fetchone()
    finally:
        conn.close()


def test_request_rebuild_queues_without_claiming_processed_picks(pg):
    week, sid, users, _ = pg
    conn = appmod.db()
    conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?, ?, 1)", (users[0], sid))
    conn.commit()
    conn.close()

    assert appmod.request_rebuild(week) == 0
    assert pending()[0] == 1
    conn = appmod.db()
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    conn.close()
    settle()
    assert appmod.process_rebuilds() == 1
    assert pending() is None
    conn = appmod.db()
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 1
    conn.close()


def test_manager_rebuild_reports_synchronous_processed_pick_count(pg):
    _, sid, users, manager = pg
    conn = appmod.db()
    conn.executemany(
        "INSERT INTO picks (user_id, shift_id, rank) VALUES (?, ?, 1)",
        [(uid, sid) for uid in users[:3]],
    )
    conn.commit()
    conn.close()

    response = client_for(manager, "manager").post("/manager/run", follow_redirects=True)
    assert response.status_code == 200
    assert b"Scheduler ran over 3 picks" in response.data
    assert pending() is None
    conn = appmod.db()
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 1
    conn.close()


def test_week_lock_skips_same_week_allows_other_week_and_releases(pg):
    week, *_ = pg
    owner = appmod.db()
    try:
        assert appmod.try_scheduler_lock(owner, week)
        assert appmod.run_scheduler(week) == 0
        other = appmod.db()
        try:
            assert appmod.try_scheduler_lock(other, "2026-10-12")
        finally:
            other.close()
    finally:
        owner.close()
    again = appmod.db()
    try:
        assert appmod.try_scheduler_lock(again, week)
        again.commit()
        assert appmod.try_scheduler_lock(again, week)
    finally:
        again.close()


def test_twenty_pick_posts_coalesce_across_workers(pg, monkeypatch):
    week, sid, users, _ = pg
    barrier = threading.Barrier(20)
    form = {f"rank_day_{day}": str(i + 1) for i, day in enumerate(appmod.DAYS)}

    def submit(uid):
        client = client_for(uid)
        barrier.wait(timeout=10)
        return client.post("/pick", data=form).status_code

    with ThreadPoolExecutor(max_workers=20) as executor:
        assert list(executor.map(submit, users)) == [302] * 20
    conn = appmod.db()
    assert conn.execute("SELECT COUNT(*) FROM picks").fetchone()[0] == 20
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    conn.close()
    assert pending()[0] == 20
    assert appmod.process_rebuilds() == 0  # trailing deadline has not settled
    calls = []
    original = debounce._schedule_week

    def counted(conn, week):
        calls.append(week)
        return original(conn, week)

    monkeypatch.setattr(debounce, "_schedule_week", counted)
    time.sleep(0.55)
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(lambda _: appmod.process_rebuilds(), range(4))) == 1
    assert calls == [week]
    assert pending() is None
    conn = appmod.db()
    assert (
        conn.execute("SELECT COUNT(*) FROM assignments WHERE shift_id=?", (sid,)).fetchone()[0] == 1
    )
    conn.close()


def test_pick_during_rebuild_is_fast_and_retained(pg, monkeypatch):
    week, _, users, _ = pg
    client = client_for(users[0])
    form = {f"rank_day_{day}": str(i + 1) for i, day in enumerate(appmod.DAYS)}
    assert client.post("/pick", data=form).status_code == 302
    settle()
    running = threading.Event()
    release = threading.Event()
    original = debounce._schedule_week

    def paused(conn, week):
        state = engine._SchedulerState(conn, week, "system")
        assert engine._begin_week(state)  # real shift locks are held
        assert (
            appmod.coverage_plan(conn, week, state.shift_ids[0], None, _shift_locked=True)
            == users[0]
        )
        running.set()
        assert release.wait(5)
        return original(conn, week)

    monkeypatch.setattr(debounce, "_schedule_week", paused)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(appmod.process_rebuilds)
        assert running.wait(5)
        try:
            start = time.monotonic()
            assert client.post("/pick", data=form).status_code == 302
            assert time.monotonic() - start < 0.2
        finally:
            release.set()
        assert result.result(timeout=5) == 1
    assert pending()[0] == 2
    monkeypatch.setattr(debounce, "_schedule_week", original)
    settle()
    assert appmod.process_rebuilds() == 1
    assert pending() is None


def test_failed_rebuild_rolls_back_and_retries_after_restart(pg, monkeypatch):
    week, sid, users, _ = pg
    conn = appmod.db()
    appmod.enqueue_rebuild(conn, week)
    conn.commit()
    conn.close()
    settle()
    original = debounce._schedule_week

    def fail(conn, week):
        conn.execute("INSERT INTO assignments (shift_id, user_id) VALUES (?, ?)", (sid, users[0]))
        raise RuntimeError("rebuild interrupted")

    monkeypatch.setattr(debounce, "_schedule_week", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        appmod.process_rebuilds()
    assert pending() is not None
    conn = appmod.db()
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 0
    conn.close()
    monkeypatch.setattr(debounce, "_schedule_week", original)
    # A new poller consumes persisted work with no in-memory enqueue state.
    worker = debounce.RebuildWorker()
    try:
        worker.start()
        deadline = time.monotonic() + 5
        while pending() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pending() is None
    finally:
        worker.stop()


def test_busy_scheduler_retains_pending_rebuild(pg):
    week, *_ = pg
    owner = appmod.db()
    assert appmod.try_scheduler_lock(owner, week)
    conn = appmod.db()
    appmod.enqueue_rebuild(conn, week)
    conn.commit()
    conn.close()
    settle()
    try:
        assert appmod.process_rebuilds() == 0
        assert pending()
    finally:
        owner.close()
    assert appmod.process_rebuilds() == 1
    assert pending() is None


def test_concurrent_coverage_cannot_claim_last_slot_twice(pg):
    week, sid, _, _ = pg
    barrier = threading.Barrier(2)

    def cover():
        conn = appmod.db()
        try:
            barrier.wait(timeout=5)
            uid = appmod.coverage_plan(conn, week, sid, None)
            if uid is not None:
                conn.execute(
                    "INSERT INTO assignments (shift_id, user_id) VALUES (?, ?)", (sid, uid)
                )
            conn.commit()
            return uid
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: cover(), range(2)))
    assert sum(uid is not None for uid in results) == 1


def test_concurrent_manager_assignment_cannot_overfill_or_reactivate_absent(pg, monkeypatch):
    week, sid, users, manager = pg
    monkeypatch.setattr("shiftwise.routes.conflicts.run_scheduler", lambda _: 0)
    barrier = threading.Barrier(2)

    def assign(uid):
        client = client_for(manager, "manager")
        barrier.wait(timeout=5)
        return client.post(f"/manager/assign/{sid}/{uid}").status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(assign, users[:2])) == [302, 302]
    conn = appmod.db()
    assert conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0] == 1
    conn.execute(
        "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'sick')", (sid, users[2])
    )
    conn.commit()
    conn.close()
    response = client_for(manager, "manager").post(
        f"/manager/assign/{sid}/{users[2]}", follow_redirects=True
    )
    assert b"already full" in response.data
    conn = appmod.db()
    assert (
        conn.execute("SELECT status FROM assignments WHERE user_id=?", (users[2],)).fetchone()[0]
        == "sick"
    )
    conn.close()


def test_dashboard_does_not_write_and_read_all_is_user_scoped(pg):
    _, _, users, _ = pg
    conn = appmod.db()
    for uid in users[:2]:
        conn.execute(
            "INSERT INTO notifications (user_id, kind, message) VALUES (?, 'assignment', 'Test')",
            (uid,),
        )
    conn.commit()
    conn.close()
    client = client_for(users[0])
    assert client.get("/").status_code == 200
    conn = appmod.db()
    assert not any(r[0] for r in conn.execute("SELECT read FROM notifications"))
    conn.close()
    assert client.get("/notifications/read-all").status_code == 405
    assert client.post("/notifications/read-all").status_code == 302
    conn = appmod.db()
    assert conn.execute("SELECT read FROM notifications WHERE user_id=?", (users[0],)).fetchone()[0]
    assert not conn.execute(
        "SELECT read FROM notifications WHERE user_id=?", (users[1],)
    ).fetchone()[0]
    conn.close()


def test_vacation_backfill_cannot_exceed_employee_hours(pg):
    week, sid, users, _ = pg
    conn = appmod.db()
    conn.execute("UPDATE users SET station='back' WHERE role='employee'")
    conn.execute("UPDATE users SET station='front', weekly_hours=1 WHERE id IN (?, ?)", users[:2])
    conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
        "VALUES (?, 'Tue', '09:00', '10:00', 1)",
        (week,),
    )
    tuesday = conn.execute("SELECT id FROM shifts WHERE day='Tue'").fetchone()[0]
    conn.execute(
        "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'manager_fixed')",
        (tuesday, users[0]),
    )
    conn.execute(
        "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'confirmed')",
        (sid, users[1]),
    )
    conn.execute(
        "INSERT INTO requests (user_id, kind, status, vacation_start, vacation_end) "
        "VALUES (?, 'vacation', 'approved', ?, ?)",
        (users[1], week, week),
    )
    conn.commit()
    conn.close()
    appmod.run_scheduler(week)
    conn = appmod.db()
    assert (
        conn.execute("SELECT COUNT(*) FROM assignments WHERE shift_id=?", (sid,)).fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM assignments WHERE user_id=?", (users[0],)).fetchone()[0]
        == 1
    )
    conn.close()


def test_vacation_backfill_cannot_consume_required_day_off(pg):
    week, sid, users, _ = pg
    conn = appmod.db()
    conn.execute("UPDATE users SET station='back' WHERE role='employee'")
    conn.execute("UPDATE users SET station='front', weekly_hours=40 WHERE id IN (?, ?)", users[:2])
    for day in ("Tue", "Wed", "Thu", "Fri", "Sat"):
        shift = conn.execute(
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
            "VALUES (?, ?, '09:00', '10:00', 1) RETURNING id",
            (week, day),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'manager_fixed')",
            (shift, users[0]),
        )
    conn.execute(
        "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'confirmed')",
        (sid, users[1]),
    )
    conn.execute(
        "INSERT INTO requests (user_id, kind, status, vacation_start, vacation_end) "
        "VALUES (?, 'vacation', 'approved', ?, ?)",
        (users[1], week, week),
    )
    conn.commit()
    conn.close()

    appmod.run_scheduler(week)
    conn = appmod.db()
    assert (
        conn.execute("SELECT COUNT(*) FROM assignments WHERE shift_id=?", (sid,)).fetchone()[0] == 0
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM assignments WHERE user_id=?", (users[0],)).fetchone()[0]
        == 5
    )
    conn.close()


def test_coverage_on_different_shifts_serializes_employee_hours(pg):
    week, sid, users, _ = pg
    conn = appmod.db()
    conn.execute("UPDATE users SET station='back' WHERE role='employee'")
    conn.execute("UPDATE users SET station='front', weekly_hours=1 WHERE id=?", (users[0],))
    conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
        "VALUES (?, 'Tue', '09:00', '10:00', 1)",
        (week,),
    )
    target = conn.execute("SELECT id FROM shifts WHERE day='Tue'").fetchone()[0]
    conn.commit()
    try:
        assert appmod.coverage_plan(conn, week, sid, None) == users[0]
        competitor = appmod.db()
        try:
            # No assignment exists yet, but the candidate is leased to the first writer.
            assert appmod.coverage_plan(competitor, week, target, None) is None
        finally:
            competitor.close()
        conn.execute("INSERT INTO assignments (shift_id, user_id) VALUES (?, ?)", (sid, users[0]))
        conn.commit()
        assert appmod.coverage_plan(conn, week, target, None) is None
    finally:
        conn.close()


@pytest.mark.parametrize("has_request", [False, True])
def test_shift_delete_locks_possible_swap_pairs_in_order(pg, monkeypatch, has_request):
    week, sid, users, manager = pg
    conn = appmod.db()
    conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots) "
        "VALUES (?, 'Tue', '09:00', '10:00', 1)",
        (week,),
    )
    target = conn.execute("SELECT id FROM shifts WHERE day='Tue'").fetchone()[0]
    if has_request:
        conn.execute(
            "INSERT INTO requests (user_id, kind, shift_id, target_shift_id, target_user_id) "
            "VALUES (?, 'swap', ?, ?, ?)",
            (users[0], sid, target, users[1]),
        )
    conn.commit()
    appmod.lock_shifts(conn, [sid])
    ready = threading.Event()
    backend = []
    original_db = appmod.db

    def tracked_db():
        connection = original_db()
        backend.append(connection.info.backend_pid)
        ready.set()
        return connection

    monkeypatch.setattr("shiftwise.routes.manager.db", tracked_db)
    client = client_for(manager, "manager")
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(client.post, f"/manager/shift/delete/{target}")
        try:
            assert ready.wait(5)
            deadline = time.monotonic() + 5
            waiting = False
            while time.monotonic() < deadline:
                with psycopg.connect(TEST_URL, autocommit=True) as observer:
                    waiting = observer.execute(
                        "SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=%s",
                        (backend[0],),
                    ).fetchone()[0]
                if waiting:
                    break
                time.sleep(0.01)
            assert waiting
            probe = original_db()
            try:
                # The delete waits at the lower ID before acquiring its target.
                probe.execute("SELECT id FROM shifts WHERE id=? FOR UPDATE NOWAIT", (target,))
            finally:
                probe.close()
        finally:
            conn.close()
        assert result.result(timeout=5).status_code == 302
