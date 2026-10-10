from datetime import date, timedelta

import app as appmod


def test_employee_coverage_willingness_is_saved_and_honored():
    appmod.init_db(seed_demo=True)
    client = appmod.app.test_client()
    response = client.post("/login", data={"username": "alex", "password": "alex"})
    assert response.status_code == 302

    week = appmod.monday_of(date.today()).isoformat()
    conn = appmod.db()
    shift_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM shifts WHERE week_start=? AND area='front' ORDER BY id", (week,)
        )
    ]
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    conn.execute("UPDATE users SET station='back' WHERE role='employee' AND id!=?", (alex_id,))
    conn.commit()
    conn.close()
    assert len(shift_ids) >= 2

    form = {}
    for rank, shift_id in enumerate(shift_ids, 1):
        form[f"rank_{shift_id}"] = str(rank)
        form[f"cover_{shift_id}"] = "yes" if rank == 1 else "no"
    response = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved for the posted week." in response.data

    conn = appmod.db()
    saved = conn.execute(
        "SELECT willing FROM coverage_preferences WHERE user_id=? AND shift_id=?",
        (alex_id, shift_ids[0]),
    ).fetchone()
    refused = conn.execute(
        "SELECT willing FROM coverage_preferences WHERE user_id=? AND shift_id=?",
        (alex_id, shift_ids[1]),
    ).fetchone()
    assert saved and saved["willing"] == 1
    assert refused and refused["willing"] == 0
    conn.execute("DELETE FROM assignments")
    conn.commit()
    assert appmod.coverage_plan(conn, week, shift_ids[1], out_uid=-1) is None
    assert appmod.coverage_plan(conn, week, shift_ids[0], out_uid=-1) == alex_id
    conn.close()

    page = client.get("/calendar").data
    text = page
    assert b"Preferred shifts" in text
    assert b"Availability to cover" in text
    assert b"Willing to cover" not in text
    assert b"Monday" in text and b"Sunday" in text
    assert b'type="checkbox" name="cover_' in text


def test_preferred_shift_ranking_matches_cover_availability_format():
    appmod.init_db(seed_demo=True)
    client = appmod.app.test_client()
    client.post("/login", data={"username": "alex", "password": "alex"})
    week = appmod.monday_of(date.today()).isoformat()
    conn = appmod.db()
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    conn.execute("UPDATE users SET station='back' WHERE role='employee' AND id!=?", (alex_id,))
    conn.execute(
        "INSERT INTO shifts (week_start, day, start_time, end_time, slots, area) "
        "VALUES (?, 'Mon', '11:00', '19:00', 1, 'front')",
        (week,),
    )
    conn.commit()
    conn.close()

    conn = appmod.db()
    shift_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM shifts WHERE week_start=? AND area='front' ORDER BY id", (week,)
        )
    ]
    conn.close()
    form = {f"rank_{shift_id}": str(rank) for rank, shift_id in enumerate(shift_ids, 1)}
    response = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved" in response.data
    conn = appmod.db()
    saved = conn.execute(
        "SELECT shift_id, rank FROM picks WHERE user_id=? ORDER BY rank", (alex_id,)
    ).fetchall()
    assert len(saved) == len(shift_ids)
    assert [row["rank"] for row in saved] == list(range(1, len(shift_ids) + 1))
    conn.close()

    page = client.get("/calendar").data
    assert b"Preferred shifts" in page
    assert page.count(b'class="availability-day shift-rank-option"') == 7
    assert page.count(b'name="rank_') == len(shift_ids)
    assert b"Rank every shift once" in page


def test_employee_can_save_independent_preferences_for_two_weeks():
    appmod.init_db(seed_demo=True)
    import mock_seed

    mock_seed.seed(appmod, force=True)
    client = appmod.app.test_client()
    client.post("/login", data={"username": "alex", "password": "alex"})
    week0 = appmod.monday_of(date.today())
    weeks = [week0.isoformat(), (week0 + timedelta(days=7)).isoformat()]
    conn = appmod.db()
    shifts_by_week = {
        week: [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM shifts WHERE week_start=? AND area='front' ORDER BY id", (week,)
            )
        ]
        for week in weeks
    }
    alex_id = conn.execute("SELECT id FROM users WHERE username='alex'").fetchone()["id"]
    conn.close()
    form = {}
    for week_index, week in enumerate(weeks):
        for rank, shift_id in enumerate(shifts_by_week[week], 1):
            form[f"rank_{shift_id}"] = str(
                rank if week_index == 0 else len(shifts_by_week[week]) + 1 - rank
            )
    response = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved" in response.data
    conn = appmod.db()
    try:
        for index, week in enumerate(weeks):
            ranks = [
                row["rank"]
                for row in conn.execute(
                    "SELECT p.rank FROM picks p JOIN shifts s ON s.id=p.shift_id "
                    "WHERE p.user_id=? AND s.week_start=? ORDER BY s.id",
                    (alex_id, week),
                )
            ]
            expected = list(range(1, 22)) if index == 0 else list(range(21, 0, -1))
            assert ranks == expected
        saved_before_invalid = [
            tuple(row)
            for row in conn.execute(
                "SELECT s.week_start, p.shift_id, p.rank FROM picks p "
                "JOIN shifts s ON s.id=p.shift_id WHERE p.user_id=? "
                "ORDER BY s.week_start, p.shift_id",
                (alex_id,),
            )
        ]
        page = client.get("/calendar").data
        assert page.count(b"Preferred shifts") == 1
        assert weeks[0].encode() in page and weeks[1].encode() in page
        assert page.count(b'name="rank_') == 42

        incomplete = dict(form)
        incomplete.pop(f"rank_{shifts_by_week[weeks[1]][-1]}")
        rejected = client.post("/pick", data=incomplete, follow_redirects=True)
        assert b"Rank every shift in each scheduled week." in rejected.data
        saved_after_invalid = [
            tuple(row)
            for row in conn.execute(
                "SELECT s.week_start, p.shift_id, p.rank FROM picks p "
                "JOIN shifts s ON s.id=p.shift_id WHERE p.user_id=? "
                "ORDER BY s.week_start, p.shift_id",
                (alex_id,),
            )
        ]
        assert saved_after_invalid == saved_before_invalid
    finally:
        conn.close()


def test_preferred_shift_ranking_pages_one_week_at_a_time():
    appmod.init_db(seed_demo=True)
    import mock_seed

    mock_seed.seed(appmod, force=True)
    client = appmod.app.test_client()
    client.post("/login", data={"username": "alex", "password": "alex"})
    week0 = appmod.monday_of(date.today())
    weeks = [week0.isoformat(), (week0 + timedelta(days=7)).isoformat()]

    page = client.get("/calendar").get_data(as_text=True)
    assert 'id="preference-week-pager"' in page
    assert 'aria-label="Next week"' in page
    assert all(f'data-preference-week-panel="{week}"' in page for week in weeks)
    assert page.count('class="availability-day shift-rank-option"') == 14
    assert page.count('name="rank_') == 42
    assert "Sunday" in page
    assert 'aria-live="polite"' in page
