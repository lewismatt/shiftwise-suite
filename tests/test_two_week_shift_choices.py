from collections import defaultdict
from datetime import date, timedelta
from html.parser import HTMLParser

from test_support import insert_id

import app as appmod


class WeekScopedForms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.form = None
        self.select_name = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("data-week"):
            self.form = {
                "kind": attrs.get("data-kind"),
                "week": attrs["data-week"],
                "options": defaultdict(list),
            }
        elif self.form and tag == "select":
            self.select_name = attrs.get("name")
        elif self.form and tag == "option" and self.select_name:
            self.form["options"][self.select_name].append(attrs.get("data-week"))

    def handle_endtag(self, tag):
        if tag == "select":
            self.select_name = None
        elif tag == "form" and self.form:
            self.forms.append(self.form)
            self.form = None
            self.select_name = None


def test_coworker_swap_forms_only_offer_same_week_shifts():
    users, (alex_current, sam_current) = _setup_pair()
    current_week = appmod.monday_of(date.today())
    next_week = (current_week + timedelta(days=7)).isoformat()
    current_week = current_week.isoformat()

    conn = appmod.db()
    next_alex = insert_id(
        conn,
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
        "VALUES (?, 'Tue', '01:00', '03:00', 1, 'front')",
        (next_week,),
    )
    next_sam = insert_id(
        conn,
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
        "VALUES (?, 'Tue', '03:00', '05:00', 1, 'front')",
        (next_week,),
    )
    for week in (current_week, next_week):
        insert_id(
            conn,
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
            "VALUES (?, 'Wed', '09:00', '11:00', 1, 'front')",
            (week,),
        )
    conn.executemany(
        "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'notified')",
        [(next_alex, users["alex"]), (next_sam, users["sam"])],
    )
    conn.commit()
    conn.close()

    client = appmod.app.test_client()
    response = client.post("/login", data={"username": "alex", "password": "alex"})
    assert response.status_code == 302
    page = client.get("/")
    parser = WeekScopedForms()
    parser.feed(page.get_data(as_text=True))

    forms_by_kind = defaultdict(list)
    for form in parser.forms:
        forms_by_kind[form["kind"]].append(form)
    assert "switch" not in forms_by_kind
    assert len(forms_by_kind["swap"]) == 2
    for form in forms_by_kind["swap"]:
        assert form["options"]["shift_id"]
        assert form["options"]["target_assignment"]
        assert set(form["options"]["shift_id"]) == {form["week"]}
        assert set(form["options"]["target_assignment"]) == {form["week"]}


def _setup_pair():
    appmod.init_db(seed_demo=True)
    week = appmod.monday_of(date.today()).isoformat()
    conn = appmod.db()
    users = {
        row["username"]: row["id"]
        for row in conn.execute("SELECT id, username FROM users WHERE username IN ('alex','sam')")
    }
    shifts = []
    for start, end in (("01:00", "03:00"), ("03:00", "05:00")):
        shifts.append(
            insert_id(
                conn,
                "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
                "VALUES (?, 'Mon', ?, ?, 1, 'front')",
                (week, start, end),
            )
        )
    conn.executemany(
        "INSERT INTO assignments (shift_id, user_id, status) VALUES (?, ?, 'notified')",
        [(shifts[0], users["alex"]), (shifts[1], users["sam"])],
    )
    conn.commit()
    conn.close()
    return users, shifts
