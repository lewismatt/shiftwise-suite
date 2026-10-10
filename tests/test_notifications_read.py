"""Dashboard reads and explicit notification acknowledgment."""

from flask.testing import FlaskClient

import app as appmod


def test_dashboard_get_preserves_unread_and_post_marks_only_current_user():
    appmod.init_db(seed_demo=True)
    conn = appmod.db()
    users = [
        r[0] for r in conn.execute("SELECT id FROM users WHERE role='employee' ORDER BY id LIMIT 2")
    ]
    for uid in users:
        appmod.notify(conn, uid, "assignment", "Unread notification")
    conn.commit()
    conn.close()
    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session.update(uid=users[0], role="employee", name="Test")
    dashboard = client.get("/").data
    assert b"Mark all as read" not in dashboard
    assert b"Unread notification" not in dashboard
    assert b"Mark all as read" in client.get("/notifications").data
    conn = appmod.db()
    assert not any(row[0] for row in conn.execute("SELECT read FROM notifications"))
    conn.close()
    assert client.get("/notifications/read-all").status_code == 405
    response = client.post("/notifications/read-all")
    assert response.status_code == 302
    assert response.location.endswith("/notifications")
    conn = appmod.db()
    assert (
        conn.execute("SELECT read FROM notifications WHERE user_id=?", (users[0],)).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT read FROM notifications WHERE user_id=?", (users[1],)).fetchone()[0]
        == 0
    )
    conn.close()


def test_notifications_page_is_authenticated_and_user_scoped():
    appmod.init_db(seed_demo=True)
    conn = appmod.db()
    employee = conn.execute(
        "SELECT id, username, role, name FROM users WHERE role='employee' ORDER BY id LIMIT 1"
    ).fetchone()
    manager = conn.execute(
        "SELECT id, username, role, name FROM users WHERE role='manager' ORDER BY id LIMIT 1"
    ).fetchone()
    assert employee is not None and manager is not None
    appmod.notify(conn, employee["id"], "assignment", "Employee-only notice")
    appmod.notify(conn, manager["id"], "conflict", "Manager-only notice")
    conn.commit()
    conn.close()

    for user, own_message, other_message in (
        (employee, b"Employee-only notice", b"Manager-only notice"),
        (manager, b"Manager-only notice", b"Employee-only notice"),
    ):
        client = appmod.app.test_client()
        with client.session_transaction() as session:
            session.update(uid=user["id"], role=user["role"], name=user["name"], station="front")
        response = client.get("/notifications")
        assert response.status_code == 200
        assert own_message in response.data
        assert other_message not in response.data
        assert response.data.count(b'href="/notifications"') == 2
        assert response.data.count(b'aria-label="1 unread notifications"') == 2

    anonymous = appmod.app.test_client().get("/notifications")
    assert anonymous.status_code == 302
    assert "/login" in anonymous.location


def test_notifications_page_includes_history_beyond_the_latest_twenty():
    appmod.init_db(seed_demo=True)
    conn = appmod.db()
    employee = conn.execute(
        "SELECT id, username, name FROM users WHERE role='employee' ORDER BY id LIMIT 1"
    ).fetchone()
    assert employee is not None
    for index in range(25):
        appmod.notify(conn, employee["id"], "assignment", f"History item {index}")
    conn.commit()
    conn.close()

    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session.update(uid=employee["id"], role="employee", name=employee["name"], station="front")
    response = client.get("/notifications")
    assert response.status_code == 200
    assert b"History item 0" in response.data
    assert b"History item 24" in response.data
    assert response.data.count(b'class="notif unread assignment"') == 25


def test_acknowledgment_requires_authentication_and_csrf():
    appmod.init_db(seed_demo=True)
    client = FlaskClient(appmod.app)
    assert client.post("/notifications/read-all").status_code == 400
    client.get("/login")
    with client.session_transaction() as session:
        csrf = session["_csrf"]
    response = client.post("/notifications/read-all", data={"_csrf": csrf})
    assert response.status_code == 302
    assert response.location.endswith("/login")


def test_users_can_delete_only_their_own_notifications_individually_or_all():
    appmod.init_db(seed_demo=True)
    conn = appmod.db()
    users = conn.execute(
        "SELECT id, name FROM users WHERE role='employee' ORDER BY id LIMIT 2"
    ).fetchall()
    appmod.notify(conn, users[0]["id"], "assignment", "Delete just this notice")
    appmod.notify(conn, users[0]["id"], "conflict", "Delete with all")
    appmod.notify(conn, users[1]["id"], "assignment", "Keep another user's notice")
    first_id = conn.execute(
        "SELECT id FROM notifications WHERE user_id=? AND message=?",
        (users[0]["id"], "Delete just this notice"),
    ).fetchone()["id"]
    other_id = conn.execute(
        "SELECT id FROM notifications WHERE user_id=? AND message=?",
        (users[1]["id"], "Keep another user's notice"),
    ).fetchone()["id"]
    conn.commit()
    conn.close()

    client = appmod.app.test_client()
    with client.session_transaction() as session:
        session.update(uid=users[0]["id"], role="employee", name=users[0]["name"])
    page = client.get("/notifications")
    assert b"Delete all notifications" in page.data
    assert page.data.count(b'aria-label="Delete notification"') == 2
    response = client.post(f"/notifications/{other_id}/delete")
    assert response.status_code == 404
    response = client.post(f"/notifications/{first_id}/delete")
    assert response.status_code == 302
    assert b"Delete just this notice" not in client.get("/notifications").data

    response = client.post("/notifications/delete-all")
    assert response.status_code == 302
    assert b"No notifications yet" in client.get("/notifications").data
    conn = appmod.db()
    assert (
        conn.execute("SELECT id FROM notifications WHERE user_id=?", (users[0]["id"],)).fetchall()
        == []
    )
    assert conn.execute(
        "SELECT id FROM notifications WHERE id=? AND user_id=?", (other_id, users[1]["id"])
    ).fetchone()
    conn.close()


def test_notification_deletion_requires_authentication_and_csrf():
    appmod.init_db(seed_demo=True)
    client = FlaskClient(appmod.app)
    assert client.post("/notifications/delete-all").status_code == 400
    client.get("/login")
    with client.session_transaction() as session:
        csrf = session["_csrf"]
    response = client.post("/notifications/delete-all", data={"_csrf": csrf})
    assert response.status_code == 302
    assert response.location.endswith("/login")
