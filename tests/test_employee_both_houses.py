"""Integration coverage for employees configured for both FOH and BOH."""

from datetime import timedelta
from html.parser import HTMLParser

import app as appmod


class ScheduledShiftRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.shift_ids = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "tr" and "data-schedule-shift" in attributes:
            self.shift_ids.append(int(attributes["data-schedule-shift"]))


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
    assert response.status_code == 200 and b"Preferences saved for both weeks." in response.data

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


def test_dual_house_calendar_defaults_to_the_combined_schedule():
    appmod.init_db(seed_demo=True)
    client = appmod.app.test_client()
    client.post("/login", data={"username": "manager", "password": "manager"})
    conn = appmod.db()
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    conn.close()
    assert client.post("/manager/roster", data={f"station_{alex_id}": "both"}).status_code == 302
    client.post("/logout")
    client.post("/login", data={"username": "alex", "password": "alex"})

    combined = client.get("/calendar")
    html = combined.data.decode()
    assert combined.status_code == 200
    assert "Both houses schedule — Alex Rivera" in html
    assert html.count('class="cal-shift') == 14
    front = client.get("/calendar?area=front")
    front_html = front.data.decode()
    assert "Front of house schedule" in front_html
    assert front_html.count('class="cal-shift') == 7

    client.post("/logout")
    client.post("/login", data={"username": "manager", "password": "manager"})
    manager_all = client.get("/calendar?area=all").data.decode()
    assert "Alex Rivera" in manager_all and "Jordan Diaz" in manager_all


def test_invalid_preference_submit_returns_to_the_calendar():
    appmod.init_db(seed_demo=True)
    client = appmod.app.test_client()
    client.post("/login", data={"username": "alex", "password": "alex"})
    response = client.post("/pick", data={"rank_1": "1"})
    assert response.status_code == 302
    assert response.location.endswith("/calendar")
    followed = client.post("/pick", data={"rank_1": "1"}, follow_redirects=True)
    assert b"Rank every shift in each scheduled week." in followed.data
    assert b'id="preference-form"' in followed.data


def test_both_house_employee_can_filter_dashboard_shifts_by_house():
    appmod.init_db(seed_demo=True)
    week = appmod.monday_of(appmod.date.today()).isoformat()
    next_week = (appmod.monday_of(appmod.date.today()) + timedelta(days=7)).isoformat()
    conn = appmod.db()
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    conn.execute("UPDATE users SET station='both' WHERE id=?", (alex_id,))
    chosen = {}
    for area in ("front", "back"):
        shift = conn.execute(
            "SELECT id FROM shifts WHERE week_start IN (?,?) AND area=? ORDER BY week_start,id LIMIT 1",
            (week, next_week, area),
        ).fetchone()
        assert shift, f"seed must include a {area}-of-house shift"
        chosen[area] = shift["id"]
    conn.execute("DELETE FROM assignments WHERE user_id=?", (alex_id,))
    for shift_id in chosen.values():
        conn.execute(
            "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'notified')",
            (shift_id, alex_id),
        )
    conn.commit()
    conn.close()

    client = appmod.app.test_client()
    client.post("/login", data={"username": "alex", "password": "alex"})
    # Default view without house param shows both houses
    default_page = client.get("/")
    assert default_page.status_code == 200
    default_html = default_page.get_data(as_text=True)
    assert 'name="house"' in default_html
    assert 'class="dashboard-house-select"' in default_html
    assert '<option value="both" selected' in default_html
    assert "front and back of house" in default_html
    default_parser = ScheduledShiftRows()
    default_parser.feed(default_html)
    assert set(default_parser.shift_ids) == set(chosen.values())

    # Filtering by front and back specifically
    for area in ("front", "back"):
        page = client.get(f"/?house={area}")
        assert page.status_code == 200
        html = page.get_data(as_text=True)
        assert 'name="house"' in html
        assert 'class="dashboard-house-select"' in html
        assert f'<option value="{area}" selected' in html
        assert ("front of house (FOH)" if area == "front" else "back of house (BOH)") in html
        parser = ScheduledShiftRows()
        parser.feed(html)
        assert parser.shift_ids == [chosen[area]]

    # Explicit both selection
    both_page = client.get("/?house=both")
    assert both_page.status_code == 200
    both_html = both_page.get_data(as_text=True)
    assert '<option value="both" selected' in both_html
    both_parser = ScheduledShiftRows()
    both_parser.feed(both_html)
    assert set(both_parser.shift_ids) == set(chosen.values())

    # Invalid house param falls back to both
    invalid = client.get("/?house=invalid")
    assert invalid.status_code == 200
    invalid_html = invalid.get_data(as_text=True)
    assert '<option value="both" selected' in invalid_html
    parser = ScheduledShiftRows()
    parser.feed(invalid_html)
    assert set(parser.shift_ids) == set(chosen.values())
