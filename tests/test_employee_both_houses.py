"""Integration coverage for employees configured for both FOH and BOH."""

from datetime import timedelta

import app as appmod


def test_employee_can_rank_and_work_both_houses():
    appmod.init_db(seed_demo=True)
    week0 = appmod.monday_of(appmod.date.today())
    weeks = [week0.isoformat(), (week0 + timedelta(days=7)).isoformat()]
    conn = appmod.db()
    conn.execute("DELETE FROM shifts")
    conn.execute("DELETE FROM picks")
    conn.execute("DELETE FROM assignments")
    conn.execute("UPDATE users SET weekly_hours=40 WHERE username='alex'")
    shift_ids = []
    for week in weeks:
        for area, day, start, end in (
            ("front", "Mon", "08:00", "16:00"),
            ("back", "Tue", "10:00", "18:00"),
        ):
            shift_ids.append(
                conn.execute(
                    "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
                    "VALUES (?,?,?,?,1,?) RETURNING id",
                    (week, day, start, end, area),
                ).fetchone()["id"]
            )
    conn.commit()
    conn.close()

    client = appmod.app.test_client()
    client.post("/login", data={"username": "manager", "password": "manager"})
    conn = appmod.db()
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    conn.close()
    roster_page = client.get("/manager/roster")
    assert roster_page.status_code == 200 and b"Both FOH and BOH" in roster_page.data
    saved = client.post("/manager/roster", data={f"station_{alex_id}": "both"})
    assert saved.status_code == 302
    client.post("/logout")
    login = client.post("/login", data={"username": "alex", "password": "alex"})
    assert login.status_code == 302 and login.location == "/", (login.status_code, login.location)
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    calendar = client.get("/calendar")
    assert calendar.status_code == 200
    html = calendar.data.decode()
    assert html.count('name="rank_') == 4
    for shift_id in shift_ids:
        assert f'name="rank_{shift_id}"' in html
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    dashboard_html = dashboard.data.decode()
    assert 'name="rank_' not in dashboard_html
    assert "Request a day off" in dashboard_html
    assert "Request vacation" in dashboard_html
    assert "Request an employee swap" in dashboard_html
    form = {f"rank_{sid}": str((i % 2) + 1) for i, sid in enumerate(shift_ids)}
    response = client.post("/pick", data=form, follow_redirects=True)
    assert response.status_code == 200 and b"Preferences saved" in response.data

    for week in weeks:
        appmod.run_scheduler(week)
    conn = appmod.db()
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    assigned_areas = {
        row["area"]
        for row in conn.execute(
            "SELECT DISTINCT s.area FROM assignments a JOIN shifts s ON s.id=a.shift_id "
            "WHERE a.user_id=? AND s.week_start IN (?,?)",
            (alex_id, *weeks),
        )
    }
    picked_count = conn.execute(
        "SELECT COUNT(*) FROM picks p JOIN shifts s ON s.id=p.shift_id "
        "WHERE p.user_id=? AND s.week_start IN (?,?)",
        (alex_id, *weeks),
    ).fetchone()[0]
    conn.close()
    assert assigned_areas == {"front", "back"}, assigned_areas
    assert picked_count == 4

    client.post("/logout")
    client.post("/login", data={"username": "manager", "password": "manager"})
    roster = client.get("/manager/roster")
    assert roster.status_code == 200 and b"Both FOH and BOH" in roster.data
