"""Invite cancellation stays atomic with assignment changes and stale responses."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import pytest
from test_employee_swaps import _login, _setup_swap_pair
from test_support import insert_id

import app as appmod
from shiftwise.routes import employee


def _invite():
    users, shifts = _setup_swap_pair()
    alex = appmod.app.test_client()
    _login(alex, "alex")
    response = alex.post(
        "/request/swap",
        data={"shift_id": shifts[0], "target_assignment": f"{users['sam']}:{shifts[1]}"},
    )
    assert response.status_code == 302
    conn = appmod.db()
    req_id = conn.execute("SELECT id FROM requests WHERE kind='swap'").fetchone()[0]
    conn.close()
    return users, shifts, req_id, alex


def _assert_consistent():
    conn = appmod.db()
    try:
        markers = {
            tuple(row)
            for row in conn.execute(
                "SELECT shift_id, user_id FROM assignments WHERE status='swap_invited'"
            )
        }
        invites = [
            tuple(row)
            for row in conn.execute(
                "SELECT shift_id, user_id FROM requests WHERE kind='swap' "
                "AND target_user_id IS NOT NULL AND status='approved'"
            )
        ]
        assert markers == set(invites)
        assert len(invites) == len(markers)
    finally:
        conn.close()


@pytest.mark.parametrize(
    "action",
    ["delete_target", "delete_recipient", "unassign", "override", "sick", "self_swap", "switch"],
)
def test_assignment_and_request_invalidation_cancel_invites_together(action):
    users, (source, target), req_id, alex = _invite()
    manager = appmod.app.test_client()
    _login(manager, "manager")
    if action == "delete_target":
        response = manager.post(f"/manager/shift/delete/{target}")
    elif action == "delete_recipient":
        response = manager.post(f"/manager/roster/{users['sam']}/delete", data={"confirm": "yes"})
    elif action == "unassign":
        response = manager.post(f"/manager/unassign/{source}/{users['alex']}")
    elif action == "override":
        response = manager.post(f"/manager/assign/{source}/{users['alex']}")
    elif action == "sick":
        response = alex.post(f"/request/sick/{source}")
    elif action == "self_swap":
        response = alex.post(f"/swap/{source}")
    else:
        conn = appmod.db()
        week = appmod.monday_of(date.today()).isoformat()
        destination = insert_id(
            conn,
            "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
            "VALUES (?, 'Tue', '01:00', '03:00', 1, 'front')",
            (week,),
        )
        switch = insert_id(
            conn,
            "INSERT INTO requests (user_id, kind, shift_id, target_shift_id) "
            "VALUES (?, 'switch', ?, ?)",
            (users["alex"], source, destination),
        )
        conn.commit()
        conn.close()
        response = manager.post(f"/manager/requests/{switch}/approve")
    assert response.status_code == 302
    _assert_consistent()
    conn = appmod.db()
    try:
        request = conn.execute("SELECT status FROM requests WHERE id=?", (req_id,)).fetchone()
        assert request is None or request[0] == "superseded"
        if action in ("delete_target", "delete_recipient"):
            assert (
                conn.execute(
                    "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
                    (source, users["alex"]),
                ).fetchone()[0]
                == "confirmed"
            )
    finally:
        conn.close()


def test_old_response_cannot_decline_a_new_invite_after_waiting_for_shift_locks(monkeypatch):
    users, (source, target), old_id, alex = _invite()
    stale = appmod.app.test_client()
    current = appmod.app.test_client()
    _login(stale, "sam")
    _login(current, "sam")
    read_request = threading.Event()
    release = threading.Event()
    original = employee.lock_shifts

    def paused_lock(conn, ids):
        if threading.current_thread().name.startswith("stale-response"):
            read_request.set()
            assert release.wait(10)
        return original(conn, ids)

    monkeypatch.setattr(employee, "lock_shifts", paused_lock)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale-response") as pool:
        result = pool.submit(
            stale.post,
            f"/request/{old_id}/respond",
            data={"decision": "reject", "reason": "Stale rejection"},
        )
        try:
            assert read_request.wait(5)
            assert (
                current.post(
                    f"/request/{old_id}/respond",
                    data={"decision": "reject", "reason": "First rejection"},
                ).status_code
                == 302
            )
            assert (
                alex.post(
                    "/request/swap",
                    data={"shift_id": source, "target_assignment": f"{users['sam']}:{target}"},
                ).status_code
                == 302
            )
        finally:
            release.set()
        assert result.result(timeout=10).status_code == 404
    _assert_consistent()
    conn = appmod.db()
    try:
        assert (
            conn.execute("SELECT reason FROM requests WHERE id=?", (old_id,)).fetchone()[0]
            == "First rejection"
        )
        assert (
            conn.execute(
                "SELECT status FROM requests WHERE id!=? AND kind='swap'", (old_id,)
            ).fetchone()[0]
            == "approved"
        )
    finally:
        conn.close()


@pytest.mark.parametrize("action", ["delete_target", "unassign"])
def test_cancellation_commits_before_a_waiting_accept(monkeypatch, action):
    users, (source, target), req_id, _ = _invite()
    sam = appmod.app.test_client()
    manager = appmod.app.test_client()
    _login(sam, "sam")
    _login(manager, "manager")
    read_request = threading.Event()
    release = threading.Event()
    original = employee.lock_shifts

    def paused_lock(conn, ids):
        read_request.set()
        assert release.wait(10)
        return original(conn, ids)

    monkeypatch.setattr(employee, "lock_shifts", paused_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        response = pool.submit(sam.post, f"/request/{req_id}/respond", data={"decision": "accept"})
        try:
            assert read_request.wait(5)
            path = (
                f"/manager/shift/delete/{target}"
                if action == "delete_target"
                else f"/manager/unassign/{source}/{users['alex']}"
            )
            assert manager.post(path).status_code == 302
            _assert_consistent()
        finally:
            release.set()
        assert response.result(timeout=10).status_code == 404
    _assert_consistent()
    conn = appmod.db()
    try:
        assert (
            conn.execute("SELECT status FROM requests WHERE id=?", (req_id,)).fetchone()[0]
            == "superseded"
        )
        assert (
            conn.execute(
                "SELECT user_id FROM assignments WHERE shift_id=? AND status='switch_fixed'",
                (source,),
            ).fetchone()
            is None
        )
    finally:
        conn.close()
