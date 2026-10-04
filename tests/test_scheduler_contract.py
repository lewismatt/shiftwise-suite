"""Synchronous scheduler results remain available in the SQLite fallback."""

import app as appmod


def test_sqlite_request_rebuild_returns_processed_pick_count(isolated_db):
    appmod.init_db(seed_demo=True)
    week = appmod.monday_of(appmod.date.today()).isoformat()
    conn = appmod.db()
    uid = conn.execute("SELECT id FROM users WHERE role='employee' ORDER BY id").fetchone()[0]
    sid = conn.execute("SELECT id FROM shifts WHERE week_start=? ORDER BY id", (week,)).fetchone()[
        0
    ]
    conn.execute("INSERT INTO picks (user_id, shift_id, rank) VALUES (?, ?, 1)", (uid, sid))
    conn.commit()
    count = conn.execute(
        "SELECT COUNT(*) FROM picks p JOIN shifts s ON s.id=p.shift_id WHERE s.week_start=?",
        (week,),
    ).fetchone()[0]
    conn.close()

    assert count > 0
    assert appmod.request_rebuild(week) == count
    assert appmod.run_scheduler(week) == count
