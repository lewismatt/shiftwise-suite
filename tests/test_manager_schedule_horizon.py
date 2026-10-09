"""Manager shift creation must support both employee preference weeks."""

from datetime import date, timedelta

import app as appmod


def test_manager_can_post_a_paired_shift_for_the_next_week():
    appmod.init_db(seed_demo=True)
    next_week = (appmod.monday_of(date.today()) + timedelta(days=7)).isoformat()
    client = appmod.app.test_client()
    response = client.post("/login", data={"username": "manager", "password": "manager"})
    assert response.status_code == 302

    response = client.post(
        "/manager/shift/add",
        data={
            "week_start": next_week,
            "area": "front",
            "day": "Mon",
            "start": "05:00",
            "end": "07:00",
            "slots": "1",
            "note": "future opening",
        },
    )
    assert response.status_code == 302

    conn = appmod.db()
    try:
        rows = conn.execute(
            "SELECT area, start_time, end_time FROM shifts "
            "WHERE week_start=? AND day='Mon' AND note='future opening' ORDER BY area",
            (next_week,),
        ).fetchall()
        assert rows
        assert [(row["area"], row["start_time"], row["end_time"]) for row in rows] == [
            ("back", "05:00", "07:00"),
            ("front", "05:00", "07:00"),
        ]
    finally:
        conn.close()

    dashboard = client.get("/manager")
    assert next_week.encode() in dashboard.data
    assert b"future opening" in dashboard.data


def test_roster_update_queues_rebuilds_for_both_schedule_weeks():
    appmod.init_db(seed_demo=True)
    week0 = appmod.monday_of(date.today())
    expected_weeks = {week0.isoformat(), (week0 + timedelta(days=7)).isoformat()}
    client = appmod.app.test_client()
    client.post("/login", data={"username": "manager", "password": "manager"})

    conn = appmod.db()
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    conn.close()
    response = client.post("/manager/roster", data={f"station_{alex_id}": "both"})
    assert response.status_code == 302

    conn = appmod.db()
    try:
        queued_weeks = {
            row["week_start"] for row in conn.execute("SELECT week_start FROM scheduler_rebuilds")
        }
        assert queued_weeks == expected_weeks
    finally:
        conn.close()


def test_employee_deletion_queues_rebuilds_for_both_schedule_weeks():
    appmod.init_db(seed_demo=True)
    week0 = appmod.monday_of(date.today())
    expected_weeks = {week0.isoformat(), (week0 + timedelta(days=7)).isoformat()}
    client = appmod.app.test_client()
    client.post("/login", data={"username": "manager", "password": "manager"})

    conn = appmod.db()
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    conn.close()
    response = client.post(f"/manager/roster/{alex_id}/delete", data={"confirm": "yes"})
    assert response.status_code == 302

    conn = appmod.db()
    try:
        queued_weeks = {
            row["week_start"] for row in conn.execute("SELECT week_start FROM scheduler_rebuilds")
        }
        assert queued_weeks == expected_weeks
    finally:
        conn.close()
