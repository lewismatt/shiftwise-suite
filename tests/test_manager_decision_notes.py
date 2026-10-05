from datetime import datetime

from test_support import insert_id


def test_employee_sees_manager_note_after_approval_or_denial(isolated_db):
    appmod = isolated_db
    appmod.init_db(seed_demo=True)
    client = appmod.app.test_client()
    conn = appmod.db()
    employee_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    week = appmod.monday_of(appmod.date.today()).isoformat()
    created_at = datetime.now().isoformat()
    request_ids = {}
    for label in ("approved", "denied"):
        request_ids[label] = insert_id(
            conn,
            "INSERT INTO requests (user_id, kind, day, week_start, reason, status, created_at) "
            "VALUES (?, 'day_off', 'Mon', ?, NULL, 'approved', ?)",
            (employee_id, week, created_at),
        )
    conn.commit()
    conn.close()

    client.post("/login", data={"username": "manager", "password": "manager"})
    client.post(
        f"/manager/requests/{request_ids['approved']}/approve",
        data={"reason": "Approved; please coordinate coverage with me."},
    )
    client.post(
        f"/manager/requests/{request_ids['denied']}/deny",
        data={"reason": "Denied; minimum staffing is required that day."},
    )

    client.post("/logout")
    client.post("/login", data={"username": "alex", "password": "alex"})
    page = client.get("/my-requests")
    assert b"Approved; please coordinate coverage with me." in page.data
    assert b"Denied; minimum staffing is required that day." in page.data
