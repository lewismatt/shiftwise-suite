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
    assert b"Mark all as read" in client.get("/").data
    conn = appmod.db()
    assert not any(row[0] for row in conn.execute("SELECT read FROM notifications"))
    conn.close()
    assert client.get("/notifications/read-all").status_code == 405
    assert client.post("/notifications/read-all").status_code == 302
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
