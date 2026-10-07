import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_support import insert_id, truncate_postgres_tables

import app as appmod


def test_manager_override():
    WEEK = appmod.monday_of(appmod.date.today()).isoformat()

    def fresh():
        trunc_conn = appmod.db()
        try:
            truncate_postgres_tables(trunc_conn)
        finally:
            trunc_conn.close()
        appmod.init_db(seed_demo=True)
        conn = appmod.db()
        conn.execute("DELETE FROM shifts")
        conn.commit()
        conn.close()

    def uid_of(username):
        conn = appmod.db()
        r = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]
        conn.close()
        return r

    def add_shift(day, slots=1, area="front"):
        conn = appmod.db()
        sid = insert_id(
            conn,
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
            "VALUES (?,?,?,?,?,?)",
            (WEEK, day, "09:00", "17:00", slots, area),
        )
        conn.commit()
        conn.close()
        return sid

    def client():
        c = appmod.app.test_client()
        c.post("/login", data={"username": "manager", "password": "manager"})
        return c

    # --- 1. manager places an employee: manager_fixed row exists, survives rebuild
    fresh()
    mon = add_shift("Mon", slots=1)
    c = client()
    r = c.post(f"/manager/assign/{mon}/{uid_of('alex')}", follow_redirects=True)
    assert r.status_code == 200
    conn = appmod.db()
    row = conn.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?", (mon, uid_of("alex"))
    ).fetchone()
    assert row and row["status"] == "manager_fixed", "override must create manager_fixed row"
    conn.close()
    # rebuild keeps it
    appmod.run_scheduler(WEEK)
    conn = appmod.db()
    row = conn.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?", (mon, uid_of("alex"))
    ).fetchone()
    assert row and row["status"] == "manager_fixed", "rebuild must keep manager_fixed"
    conn.close()
    print("1. Override creates manager_fixed; rebuild keeps it: OK")

    # --- 2. cross-house placement rejected (jordan is BOH, shift is FOH)
    r = c.post(f"/manager/assign/{mon}/{uid_of('jordan')}", follow_redirects=True)
    assert r.status_code == 200
    assert "own house" in r.data.decode(), "cross-house override must be refused with a message"
    conn = appmod.db()
    n_jordan = conn.execute(
        "SELECT COUNT(*) c FROM assignments a JOIN users u ON u.id=a.user_id "
        "WHERE u.username='jordan' AND a.shift_id=?",
        (mon,),
    ).fetchone()["c"]
    assert n_jordan == 0, "cross-house override must not create an assignment"
    conn.close()
    print("2. Cross-house override rejected: OK")

    # --- 3. full shift rejected
    r = c.post(f"/manager/assign/{mon}/{uid_of('sam')}", follow_redirects=True)
    assert "already full" in r.data.decode(), "override onto a full shift must be refused"
    conn = appmod.db()
    n_sam = conn.execute(
        "SELECT COUNT(*) c FROM assignments WHERE shift_id=? AND user_id=?",
        (mon, uid_of("sam")),
    ).fetchone()["c"]
    assert n_sam == 0
    conn.close()
    print("3. Override onto a full shift rejected: OK")

    # --- 3b. a pending swap leaves the slot open; its row isn't staffed capacity
    conn = appmod.db()
    conn.execute(
        "UPDATE assignments SET status='swap_requested' WHERE shift_id=? AND user_id=?",
        (mon, uid_of("alex")),
    )
    conn.commit()
    conn.close()
    r = c.post(f"/manager/assign/{mon}/{uid_of('sam')}", follow_redirects=True)
    assert "already full" not in r.data.decode()
    # The request queues a rebuild; drive it to retire the covered swap.
    appmod.run_scheduler(WEEK)
    conn = appmod.db()
    statuses = {
        row["status"]
        for row in conn.execute("SELECT status FROM assignments WHERE shift_id=?", (mon,))
    }
    conn.close()
    assert statuses == {"manager_fixed"}, statuses  # covered swap is retired
    appmod.run_scheduler(WEEK)
    conn = appmod.db()
    statuses = {
        row["status"]
        for row in conn.execute("SELECT status FROM assignments WHERE shift_id=?", (mon,))
    }
    conn.close()
    assert statuses == {"manager_fixed"}, statuses
    print("3b. Pending swap leaves room for override and is retired on rebuild: OK")

    # --- 4. approved day-off is overridden + employee notified
    tue = add_shift("Tue", slots=1)
    conn = appmod.db()
    conn.execute(
        "INSERT INTO requests (user_id, kind, day, status, created_at) VALUES (?,?,?,?,?)",
        (
            uid_of("sam"),
            "day_off",
            "Tue",
            "approved_ok",
            appmod.datetime.now().isoformat(timespec="seconds"),
        ),
    )
    conn.commit()
    conn.close()
    r = c.post(f"/manager/assign/{tue}/{uid_of('sam')}", follow_redirects=True)
    assert r.status_code == 200
    conn = appmod.db()
    req = conn.execute(
        "SELECT status FROM requests WHERE user_id=? AND kind='day_off' AND day='Tue'",
        (uid_of("sam"),),
    ).fetchone()
    assert req and req["status"] == "overridden_by_manager", (
        "overridden day-off must be marked overridden_by_manager"
    )
    a = conn.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?", (tue, uid_of("sam"))
    ).fetchone()
    assert a and a["status"] == "manager_fixed"
    notif = conn.execute(
        "SELECT message FROM notifications WHERE user_id=? AND message LIKE '%overrides a day-off%'",
        (uid_of("sam"),),
    ).fetchone()
    assert notif and "overrides a day-off" in notif["message"], (
        "employee must be told their day-off was overridden"
    )
    conn.close()
    print("4. Approved day-off overridden + employee notified: OK")

    # --- 5. dashboard renders manager-set status
    r = c.post("/logout")
    c2 = appmod.app.test_client()
    c2.post("/login", data={"username": "sam", "password": "sam"})
    html = c2.get("/").data.decode()
    assert "manager-set" in html, "dashboard must show manager-set tag"
    print("5. Dashboard shows manager-set status: OK")

    print("\nALL MANAGER-OVERRIDE TESTS PASSED")


if __name__ == "__main__":
    test_manager_override()


def test_manager_override_hours_cap_warning():
    """Issue #58: manager override exceeding hours cap warns but doesn't block."""
    WEEK = appmod.monday_of(appmod.date.today()).isoformat()

    conn = appmod.db()
    try:
        truncate_postgres_tables(conn)
    finally:
        conn.close()
    appmod.init_db(seed_demo=True)
    conn = appmod.db()
    conn.execute("DELETE FROM shifts")
    # Give alex a tiny 8h cap so one 8h shift hits it and a second exceeds it.
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()[0]
    conn.execute("UPDATE users SET weekly_hours=8 WHERE id=?", (alex_id,))
    # Two 8h shifts (09:00-17:00).
    sids = []
    for day in ("Mon", "Tue"):
        sids.append(
            insert_id(
                conn,
                "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
                "VALUES (?,?,?,?,?,?)",
                (WEEK, day, "09:00", "17:00", 1, "front"),
            )
        )
    conn.commit()
    conn.close()

    c = appmod.app.test_client()
    c.post("/login", data={"username": "manager", "password": "manager"})

    # First override: 8h assigned, cap is 8h — no warning (not over).
    r = c.post(f"/manager/assign/{sids[0]}/{alex_id}", follow_redirects=True)
    assert r.status_code == 200
    assert "over their 8h weekly cap" not in r.data.decode()

    # Second override: 16h total > 8h cap — warning appears, assignment still happens.
    r = c.post(f"/manager/assign/{sids[1]}/{alex_id}", follow_redirects=True)
    assert r.status_code == 200
    html = r.data.decode()
    assert "over their 8h weekly cap" in html, "expected hours-cap warning in flash"
    conn = appmod.db()
    try:
        row = conn.execute(
            "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
            (sids[1], alex_id),
        ).fetchone()
    finally:
        conn.close()
    assert row and row["status"] == "manager_fixed", "override must not be blocked"
    print("hours-cap warning test passed")
