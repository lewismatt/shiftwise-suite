from datetime import date, timedelta
from html.parser import HTMLParser

import app as appmod


class DashboardShifts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scheduled = []
        self.rank_ids = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "tr" and attrs.get("data-schedule-shift"):
            self.scheduled.append(int(attrs["data-schedule-shift"]))
        elif tag == "select" and attrs.get("name", "").startswith("rank_"):
            self.rank_ids.append(int(attrs["name"].removeprefix("rank_")))


def test_employee_dashboard_lists_only_the_signed_in_employees_scheduled_shifts():
    appmod.init_db(seed_demo=True)
    week = appmod.monday_of(date.today()).isoformat()
    next_week = (appmod.monday_of(date.today()) + timedelta(days=7)).isoformat()
    conn = appmod.db()
    alex = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    shift_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM shifts WHERE week_start IN (?,?) AND area='front' ORDER BY week_start,id",
            (week, next_week),
        )
    ]
    assert len(shift_ids) > 1, "test requires both assigned and unassigned front-house shifts"
    conn.execute("DELETE FROM assignments")
    conn.execute(
        "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'notified')",
        (shift_ids[0], alex),
    )
    conn.commit()
    conn.close()

    client = appmod.app.test_client()
    response = client.post("/login", data={"username": "alex", "password": "alex"})
    assert response.status_code == 302
    page = client.get("/")
    assert page.status_code == 200
    parser = DashboardShifts()
    parser.feed(page.get_data(as_text=True))

    assert parser.scheduled == [shift_ids[0]]
    assert parser.rank_ids == []
    html = page.get_data(as_text=True)
    assert 'data-metric="assigned-shifts"' in html
    assert 'data-metric="hours-this-week"' in html
    assert 'data-metric="house"' in html
    assert "View calendar" in html
    assert "Request a day off" in html
    assert "Request vacation" in html
    assert "Request an employee swap" in html
    calendar = client.get("/calendar")
    assert calendar.status_code == 200
    calendar_parser = DashboardShifts()
    calendar_parser.feed(calendar.get_data(as_text=True))
    assert set(calendar_parser.rank_ids) == set(shift_ids), (
        "all eligible shifts must remain available for ranking in the calendar"
    )
