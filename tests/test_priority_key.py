"""Dedicated house staff outrank dual-house floaters for that house."""

from datetime import date

import app as appmod
from shiftwise.domain.rules import priority_key


def _person(station, employment_type="full_time", hired_on="2020-01-01"):
    return {
        "station": station,
        "employment_type": employment_type,
        "hired_on": hired_on,
    }


def test_dedicated_house_staff_outrank_dual_house_floaters():
    dedicated = _person("front", "part_time", "2024-01-01")
    floater = _person("both", "full_time", "2010-01-01")
    other_house = _person("back", "full_time", "2010-01-01")

    assert priority_key(dedicated, "front") < priority_key(floater, "front")
    assert priority_key(other_house, "back") < priority_key(floater, "back")
    assert priority_key(dedicated, "back")[0] == 2

    # Unscoped lineups do not arbitrarily rank front ahead of back.
    assert priority_key(_person("front"))[0] == priority_key(_person("back"))[0] == 0


def test_scheduler_assigns_dedicated_staff_ahead_of_a_senior_floater():
    appmod.init_db(seed_demo=True)
    week = appmod.monday_of(date.today()).isoformat()
    conn = appmod.db()
    conn.execute("DELETE FROM shifts")
    conn.execute("DELETE FROM picks")
    conn.execute("DELETE FROM assignments")
    conn.execute(
        "UPDATE users SET station='front', employment_type='part_time', "
        "hired_on='2024-01-01', weekly_hours=40 WHERE username='alex'"
    )
    conn.execute(
        "UPDATE users SET station='both', employment_type='full_time', "
        "hired_on='2010-01-01', weekly_hours=40 WHERE username='sam'"
    )
    shift_id = conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
        "VALUES (?, 'Mon', '09:00', '17:00', 1, 'front') RETURNING id",
        (week,),
    ).fetchone()["id"]
    user_ids = {
        row["username"]: row["id"]
        for row in conn.execute("SELECT id, username FROM users WHERE username IN ('alex', 'sam')")
    }
    conn.executemany(
        "INSERT INTO picks (user_id, shift_id, rank) VALUES (?, ?, 1)",
        [(user_ids["alex"], shift_id), (user_ids["sam"], shift_id)],
    )
    conn.commit()
    conn.close()

    assert appmod.run_scheduler(week) == 2
    conn = appmod.db()
    try:
        winner = conn.execute(
            "SELECT u.username FROM assignments a JOIN users u ON u.id=a.user_id "
            "WHERE a.shift_id=?",
            (shift_id,),
        ).fetchone()["username"]
    finally:
        conn.close()
    assert winner == "alex"
