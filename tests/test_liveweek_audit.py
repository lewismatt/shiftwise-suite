"""Keep manual overrides visible without hiding automatic hours violations."""

import json
import random
import urllib.request
from email.message import Message
from io import BytesIO
from types import SimpleNamespace

import pytest

import app as appmod
from tools import liveweek
from tools.liveweek import Recorder, World, run_audit


def test_hours_audit_distinguishes_manager_override_from_automatic_excess(isolated_db, tmp_path):
    appmod.init_db(seed_demo=True)
    conn = appmod.db()
    uid = conn.execute("SELECT id FROM users WHERE role='employee' ORDER BY id").fetchone()[0]
    conn.execute("DELETE FROM assignments")
    conn.execute("UPDATE users SET weekly_hours=1 WHERE id=?", (uid,))
    sid = conn.execute("SELECT id FROM shifts ORDER BY id").fetchone()[0]
    conn.execute(
        "INSERT INTO assignments (user_id,shift_id,status) VALUES (?,?,'manager_fixed')",
        (uid, sid),
    )
    conn.commit()
    conn.close()
    world = World(Recorder(tmp_path), appmod)
    run_audit(world, world.rec, "manual")
    issue = next(i for i in world.rec.issues if i["category"] == "hours_cap")
    assert issue["severity"] == "MEDIUM"
    assert "manager overrides" in str(issue["detail"])
    conn = appmod.db()
    conn.execute("UPDATE assignments SET status='notified'")
    conn.commit()
    conn.close()
    world.rec = Recorder(tmp_path)
    run_audit(world, world.rec, "automatic")
    assert next(i for i in world.rec.issues if i["category"] == "hours_cap")["severity"] == "HIGH"


def test_manager_review_queue_excludes_employee_owned_invites(isolated_db, tmp_path):
    appmod.init_db(seed_demo=True)
    conn = appmod.db()
    users = [r[0] for r in conn.execute("SELECT id FROM users WHERE role='employee' ORDER BY id")]
    conn.execute(
        "INSERT INTO requests (user_id, kind, created_at) VALUES (?, 'day_off', '2026-10-03')",
        (users[0],),
    )
    conn.execute(
        "INSERT INTO requests (user_id, kind, target_user_id, created_at) "
        "VALUES (?, 'swap', ?, '2026-10-03')",
        (users[0], users[1]),
    )
    conn.commit()
    conn.close()
    assert [r["kind"] for r in World(Recorder(tmp_path), appmod).pending_requests()] == ["day_off"]


@pytest.mark.parametrize("new_sick_request", [False, True])
def test_sick_swap_drill_ignores_history_but_detects_new_duplicates(
    isolated_db, tmp_path, monkeypatch, new_sick_request
):
    appmod.init_db(seed_demo=True)
    conn = appmod.db()
    uid, actor = conn.execute(
        "SELECT id, username FROM users WHERE role='employee' ORDER BY id"
    ).fetchone()
    sid = conn.execute("SELECT id FROM shifts ORDER BY id").fetchone()[0]
    conn.execute(
        "INSERT INTO assignments (user_id, shift_id, status) VALUES (?, ?, 'confirmed')",
        (uid, sid),
    )
    conn.execute(
        "INSERT INTO requests (user_id, shift_id, kind, status, created_at) "
        "VALUES (?, ?, 'swap', 'denied', '2026-10-03')",
        (uid, sid),
    )
    conn.commit()
    conn.close()

    def post(path):
        kind = "swap" if path.startswith("/swap/") else "sick"
        if kind == "swap" or new_sick_request:
            conn = appmod.db()
            conn.execute(
                "INSERT INTO requests (user_id, shift_id, kind, created_at) "
                "VALUES (?, ?, ?, '2026-10-03')",
                (uid, sid, kind),
            )
            conn.commit()
            conn.close()
        return SimpleNamespace(status=200)

    sess = SimpleNamespace(uid=uid, actor=actor, post=post)
    monkeypatch.setattr(liveweek, "fresh_session", lambda *args: sess)
    rec = Recorder(tmp_path)
    ctx = SimpleNamespace(
        world=World(rec, appmod),
        rec=rec,
        rng=random.Random(0),
        base="http://test",
        phase="drills",
        emp_sessions=[sess],
    )
    liveweek.drill_sick_vs_swap(ctx)
    assert bool(rec.issues) is new_sick_request
    if new_sick_request:
        assert rec.issues[0]["severity"] == "HIGH"


@pytest.mark.parametrize("refusal", [True, False])
def test_double_review_requires_an_explanation_for_pending_status(tmp_path, monkeypatch, refusal):
    result = SimpleNamespace(
        status=200,
        flash="No eligible coverer is available for that swap."
        if refusal
        else "Unexpected response",
    )
    monkeypatch.setattr(
        liveweek, "fresh_session", lambda *args: SimpleNamespace(post=lambda *args: result)
    )
    rec = Recorder(tmp_path)
    ctx = SimpleNamespace(
        world=SimpleNamespace(
            pending_requests=lambda: [{"id": 1, "kind": "swap"}],
            one=lambda *args: {"status": "approved"},
        ),
        rec=rec,
        rng=random.Random(0),
        base="http://test",
        phase="drills",
        mgr_sessions=[SimpleNamespace(actor="m0"), SimpleNamespace(actor="m1")],
    )
    liveweek.drill_double_review(ctx)
    assert bool(rec.issues) is not refusal
    if not refusal:
        assert rec.issues[0]["severity"] == "HIGH"


def test_redirect_timing_records_each_http_hop(tmp_path, monkeypatch):
    rec = Recorder(tmp_path)
    owner = SimpleNamespace(rec=rec, actor="employee", phase="test", _hop_start=1.0)
    handler = liveweek._MeasuredRedirectHandler(owner)
    redirected = []
    handler.parent = SimpleNamespace(open=lambda req, timeout: redirected.append(req) or "done")
    req = urllib.request.Request("http://test/before", data=b"x=1", method="POST")
    req.timeout = 45
    headers = Message()
    headers["Location"] = "/after"
    ticks = iter([1.012, 1.013])
    monkeypatch.setattr(liveweek.time, "monotonic", lambda: next(ticks))
    assert handler.http_error_302(req, BytesIO(b"redirect body"), 302, "Found", headers) == "done"
    event = json.loads((tmp_path / "eventlog.jsonl").read_text())
    assert (event["method"], event["path"], event["status"], event["ms"]) == (
        "POST",
        "/before",
        302,
        12.0,
    )
    assert redirected[0].get_method() == "GET"
    assert (owner._hop_method, owner._hop_path, owner._hop_start) == ("GET", "/after", 1.013)
