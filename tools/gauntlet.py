"""One-shot conflict gauntlet: every feature + conflict in a single
in-process run (zero network/LLM cost). Reseed -> 10x full-week picks ->
conflict events incl. edge cases -> manager review -> audit.

STRICT: every behavioral check is an assertion. A regression fails the
run with a non-zero exit code; nothing is a printed observation.
"""

import argparse
import os
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from tools.scenario_database import prepare_database

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--database-url", default=os.environ.get("SHIFTWISE_SCENARIO_DATABASE_URL"))
args = parser.parse_args()
scenario_schema = prepare_database("gauntlet", args.database_url)

import app as appmod
from mock_seed import seed
from shiftwise.security import use_csrf_aware_test_client

# The app is imported only after its isolated PostgreSQL target is configured.
appmod.init_db(seed_demo=True)
seed(appmod, force=True, week_count=1)
appmod.init_db(seed_demo=True)

use_csrf_aware_test_client(appmod.app)
client = appmod.app.test_client()
WEEK = appmod.monday_of(date.today()).isoformat()
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EMPS = None


def login(u, password=None):
    client.get("/logout")
    response = client.post(
        "/login", data={"username": u, "password": password or u}, follow_redirects=True
    )
    assert b"Log out" in response.data, u


def uid(u):
    c = appmod.db()
    r = c.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()[0]
    c.close()
    return r


def all_emps():
    c = appmod.db()
    r = [x["username"] for x in c.execute("SELECT username FROM users WHERE role='employee'")]
    c.close()
    return r


def house_of(u):
    c = appmod.db()
    r = c.execute("SELECT station FROM users WHERE username=?", (u,)).fetchone()
    c.close()
    return r["station"] if r else "front"


def sid_of(day, area=None):
    c = appmod.db()
    if area:
        r = c.execute(
            "SELECT id FROM shifts WHERE week_start=? AND day=? AND area=?", (WEEK, day, area)
        ).fetchone()
    else:
        r = c.execute("SELECT id FROM shifts WHERE week_start=? AND day=?", (WEEK, day)).fetchone()
    c.close()
    return r["id"] if r else None


def shift_ids(area):
    c = appmod.db()
    rows = c.execute(
        "SELECT id FROM shifts WHERE week_start=? AND area=? ORDER BY id",
        (WEEK, area),
    ).fetchall()
    c.close()
    return [row["id"] for row in rows]


def day_shift_ids(area):
    """Shift ids grouped by day for one house (for per-shift rank forms)."""
    c = appmod.db()
    rows = c.execute(
        "SELECT id, day FROM shifts WHERE week_start=? AND area=? ORDER BY id",
        (WEEK, area),
    ).fetchall()
    c.close()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["day"], []).append(row["id"])
    return grouped


def my(u, status="notified"):
    c = appmod.db()
    r = c.execute(
        "SELECT s.id, s.day, a.status FROM assignments a "
        "JOIN shifts s ON s.id=a.shift_id WHERE a.user_id=? AND "
        "a.status=? AND s.week_start=?",
        (uid(u), status, WEEK),
    ).fetchall()
    c.close()
    return r


def all_holdings(u):
    c = appmod.db()
    r = c.execute(
        "SELECT s.id, s.day, a.status FROM assignments a "
        "JOIN shifts s ON s.id=a.shift_id WHERE a.user_id=? AND s.week_start=?",
        (uid(u), WEEK),
    ).fetchall()
    c.close()
    return r


def assigned_hours(username):
    c = appmod.db()
    rows = c.execute(
        "SELECT s.start_time, s.end_time FROM assignments a "
        "JOIN shifts s ON s.id=a.shift_id JOIN users u ON u.id=a.user_id "
        "WHERE u.username=? AND s.week_start=? AND a.status NOT IN ('sick','swap_requested')",
        (username, WEEK),
    ).fetchall()
    c.close()
    return sum(appmod.shift_hours(r["start_time"], r["end_time"]) for r in rows)


def workdays(username):
    c = appmod.db()
    rows = c.execute(
        "SELECT DISTINCT s.day FROM assignments a "
        "JOIN shifts s ON s.id=a.shift_id JOIN users u ON u.id=a.user_id "
        "WHERE u.username=? AND s.week_start=? AND a.status NOT IN ('sick','swap_requested')",
        (username, WEEK),
    ).fetchall()
    c.close()
    return {r["day"] for r in rows}


def conflict_note_matching(username, like, after_id):
    """True iff a conflict notification for username was created after
    after_id and its message matches the LIKE pattern.

    Scoping to (username, message, id range) is what makes the stress-phase
    assertions non-vacuous: stale notes from earlier phases can never
    satisfy them.
    """
    c = appmod.db()
    r = c.execute(
        "SELECT 1 FROM notifications n JOIN users u ON u.id=n.user_id "
        "WHERE u.username=? AND n.kind='conflict' AND n.id > ? AND n.message LIKE ?",
        (username, after_id, like),
    ).fetchone()
    c.close()
    return r is not None


def full_rank_form(day_to_ids, preferred_days):
    """Per-shift rank form preferring the given days (for cap/days-off stress).

    Ranks must be exactly 1..N with no gaps (the pick route validates this).
    A running offset keeps ranks collision-free even when days have unequal
    shift counts.
    """
    order = list(preferred_days) + [d for d in DAYS if d not in preferred_days]
    form = {}
    rank = 0
    for day in order:
        for sid in day_to_ids.get(day, []):
            rank += 1
            form[f"rank_{sid}"] = str(rank)
    return form


def schedule():
    c = appmod.db()
    rows = c.execute(
        "SELECT s.day, s.slots, COUNT(a.id) n, string_agg(u.name, ', ') names "
        "FROM shifts s LEFT JOIN assignments a ON a.shift_id=s.id "
        "AND a.status NOT IN ('sick','swap_requested') "
        "LEFT JOIN users u ON u.id=a.user_id "
        "WHERE s.week_start=? GROUP BY s.id, s.day, s.slots",
        (WEEK,),
    ).fetchall()
    c.close()
    return [(r["day"], r["n"], r["slots"], r["names"] or "-") for r in rows]


def ph(n, t):
    print(f"\n===== PHASE {n}: {t} =====")


def run_gauntlet():
    global EMPS
    # ---------- 1. full-week picks, everyone ----------
    EMPS = all_emps()
    ph(1, "all " + str(len(EMPS)) + " employees rank all 7 days (required)")
    for u in EMPS:
        login(u)
        order = DAYS[::-1] if u in ("riley", "priya") else DAYS
        form = {f"rank_day_{day}": str(i + 1) for i, day in enumerate(order)}
        r = client.post("/pick", data=form, follow_redirects=True)
        assert b"Preferences saved" in r.data, u
    print("  all saved; schedule:")
    # The rebuild is debounced; finish it before the understaffed assertion.
    appmod.run_scheduler(WEEK)
    sched = schedule()
    for d, n, s, names in sched:
        print(f"    {d}: {n}/{s}  [{names}]")
    understaffed = [f"{d} {n}/{s}" for d, n, s, _ in sched if n < s]
    assert not understaffed, f"shifts left understaffed after full picks: {understaffed}"

    # ---------- 2. partial pick rejection ----------
    ph(2, "partial day-rank form REJECTED (only 6 days ranked)")
    login("maria")
    form = {f"rank_day_{day}": str(i + 1) for i, day in enumerate(DAYS[:6])}
    r = client.post("/pick", data=form, follow_redirects=True)
    assert b"Rank all seven days" in r.data, "partial pick form was NOT rejected"

    # ---------- 3. sick call, two on same day ----------
    ph(3, "sick calls: maria + taylor both out on Monday")
    called_sick = []
    for u in ("maria", "taylor"):
        login(u)
        m = [x for x in my(u) if x["day"] == "Mon"]
        if not m:
            print(f"  {u} holds no Monday shift — skipped")
            continue
        sid = m[0]["id"]
        client.post(f"/request/sick/{sid}", follow_redirects=True)
        c = appmod.db()
        row = c.execute(
            "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
            (sid, uid(u)),
        ).fetchone()
        assert row and row["status"] == "sick", f"{u}: sick call did not mark assignment sick"
        cover = c.execute(
            "SELECT u.username FROM assignments a JOIN users u ON u.id=a.user_id "
            "WHERE a.shift_id=? AND a.user_id!=? AND a.status='notified'",
            (sid, uid(u)),
        ).fetchone()
        if not cover:
            mgr = c.execute("SELECT id FROM users WHERE role='manager'").fetchone()
            alert = c.execute(
                "SELECT 1 FROM notifications WHERE user_id=? AND kind='conflict' "
                "AND message LIKE 'No cover available%'",
                (mgr["id"],),
            ).fetchone()
            assert alert, f"{u} sick on Mon: no coverer assigned and no manager alert"
            print(f"  {u} sick on Mon -> no coverer; manager alerted")
        else:
            print(f"  {u} sick on Mon -> covered by {cover['username']}")
        c.close()
        called_sick.append(u)
    assert called_sick, "nobody called in sick — phase tested nothing"
    print("  Mon now:", [f"{n}/{s}" for d, n, s, _ in schedule() if d == "Mon"])

    # ---------- 4. swap ----------
    ph(4, "swap: devon gives up a shift (auto-covered)")
    login("devon")
    d_mine = my("devon")
    assert d_mine, "devon holds no shift to swap"
    tgt = d_mine[0]
    client.post(f"/swap/{tgt['id']}", follow_redirects=True)
    c = appmod.db()
    row = c.execute(
        "SELECT status FROM assignments WHERE shift_id=? AND user_id=?",
        (tgt["id"], uid("devon")),
    ).fetchone()
    if row is None:
        cover = c.execute(
            "SELECT u.username FROM assignments a JOIN users u ON u.id=a.user_id "
            "WHERE a.shift_id=? AND a.status IN ('notified','coverage_fixed')",
            (tgt["id"],),
        ).fetchone()
        assert cover, "devon swapped out but nobody holds the shift"
        req = c.execute(
            "SELECT 1 FROM requests WHERE user_id=? AND kind='swap' AND shift_id=? "
            "AND status='approved_ok'",
            (uid("devon"), tgt["id"]),
        ).fetchone()
        assert req, "swap covered but no approved_ok request row recorded"
        print(f"  devon released {tgt['day']}; covered by {cover['username']}")
    else:
        assert row["status"] == "swap_requested", (
            f"devon swap left unexpected status {row['status']}"
        )
        print(f"  devon released {tgt['day']}; swap_requested, manager alerted")
    c.close()

    # ---------- 5. day off ----------
    ph(5, "day off: priya drops Wednesday")
    login("priya")
    client.post("/request/day_off", data={"day": "Wed"}, follow_redirects=True)
    wed_left = [x for x in all_holdings("priya") if x["day"] == "Wed"]
    assert not wed_left, f"priya still holds Wednesday shifts after day off: {wed_left}"
    print("  Wed now:", [f"{n}/{s}" for d, n, s, _ in schedule() if d == "Wed"])

    # ---------- 6. vacation ----------
    ph(6, "vacation: alex out Tue->next Mon")
    login("alex")
    today = date.today()
    vs = today + timedelta(days=(1 if today.weekday() < 1 else 0))
    ve = vs + timedelta(days=6)
    r = client.post(
        "/request/vacation",
        data={"vac_start": vs.isoformat(), "vac_end": ve.isoformat()},
        follow_redirects=True,
    )
    assert b"Vacation requested" in r.data, "vacation request was not accepted"
    # Finish the debounced rebuild before checking in-vacation holdings.
    appmod.run_scheduler(WEEK)
    c = appmod.db()
    req = c.execute(
        "SELECT vacation_start, vacation_end FROM requests WHERE user_id=? AND kind='vacation' "
        "ORDER BY id DESC LIMIT 1",
        (uid("alex"),),
    ).fetchone()
    assert (
        req and req["vacation_start"] == vs.isoformat() and req["vacation_end"] == ve.isoformat()
    ), "vacation request row missing or has wrong range"
    held = c.execute(
        "SELECT s.day FROM assignments a JOIN shifts s ON s.id=a.shift_id "
        "JOIN users u ON u.id=a.user_id WHERE u.username='alex' AND s.week_start=?",
        (WEEK,),
    ).fetchall()
    c.close()
    base = date.fromisoformat(WEEK)
    in_range = [r["day"] for r in held if vs <= base + timedelta(days=DAYS.index(r["day"])) <= ve]
    assert not in_range, f"alex still holds in-vacation shifts: {in_range}"
    print(f"  alex vacation {vs}..{ve} recorded; in-range shifts dropped")

    # ---------- 7. vacation, past-dated (rejected) ----------
    ph(7, "vacation with past start -> rejected")
    login("jordan")
    r = client.post(
        "/request/vacation",
        data={"vac_start": "2026-09-01", "vac_end": "2026-09-05"},
        follow_redirects=True,
    )
    assert b"past" in r.data, "past-dated vacation was NOT rejected"

    # ---------- 8. switch + manager deny ----------
    ph(8, "switch request into a FULL shift -> manager approval auto-denies")
    login("sam")
    s_mine = my("sam")
    assert s_mine, "sam holds no shift to switch from"
    sam_station = house_of("sam")
    full = (
        appmod.db()
        .execute(
            "SELECT s.id, s.day FROM shifts s WHERE s.week_start=? AND s.area=? AND (SELECT "
            "COUNT(*) FROM assignments a WHERE a.shift_id=s.id AND a.status NOT IN ('sick','swap_requested')) "
            ">= s.slots ORDER BY s.id LIMIT 1",
            (WEEK, sam_station),
        )
        .fetchone()
    )
    mine_ids = {x["id"] for x in s_mine}
    assert full and full["id"] not in mine_ids, "no full shift available to test switch-into-full"
    client.post(
        f"/request/switch/{s_mine[0]['id']}",
        data={"target_shift": full["id"]},
        follow_redirects=True,
    )
    login("manager")
    c = appmod.db()
    req = c.execute(
        "SELECT id FROM requests WHERE kind='switch' AND status='approved' "
        "AND user_id=? AND shift_id=? ORDER BY id DESC LIMIT 1",
        (uid("sam"), s_mine[0]["id"]),
    ).fetchone()
    assert req, "switch request was blocked up front instead of reaching review"
    client.post(f"/manager/requests/{req['id']}/approve", follow_redirects=True)
    row = c.execute("SELECT status FROM requests WHERE id=?", (req["id"],)).fetchone()
    assert row["status"] == "denied", (
        f"switch into full shift was not denied (status={row['status']})"
    )
    kept = c.execute(
        "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
        (s_mine[0]["id"], uid("sam")),
    ).fetchone()
    c.close()
    assert kept, "sam lost his original shift after the denied switch"
    print(f"  switch into {full['day']} (full): denied; sam keeps his shift")

    # ---------- 9. manager unassign + instant backfill ----------
    ph(9, "manager unassigns jordan -> slot backfills instantly")
    login("manager")
    j_mine = my("jordan")
    assert j_mine, "jordan holds no shift to unassign"
    tgt = j_mine[0]
    client.post(f"/manager/unassign/{tgt['id']}/{uid('jordan')}", follow_redirects=True)
    # Finish the debounced rebuild before checking backfill.
    appmod.run_scheduler(WEEK)
    c = appmod.db()
    gone = c.execute(
        "SELECT 1 FROM assignments WHERE shift_id=? AND user_id=?",
        (tgt["id"], uid("jordan")),
    ).fetchone()
    assert not gone, "jordan still holds the shift after unassign"
    who = c.execute(
        "SELECT u.name FROM assignments a JOIN users u ON u.id=a.user_id "
        "WHERE a.shift_id=? AND a.user_id!=? AND a.status='notified' "
        "ORDER BY a.id DESC LIMIT 1",
        (tgt["id"], uid("jordan")),
    ).fetchone()
    c.close()
    assert who, f"shift {tgt['day']} was not backfilled after unassign"
    print(f"  jordan pulled off {tgt['day']}; backfilled by {who['name']}")

    # ---------- 10. roster change ----------
    ph(10, "roster change: add new part-timer (avery), they pick all 21 shifts")
    login("manager")
    client.post(
        "/manager/roster/add",
        data={
            "username": "avery",
            "password": "avery-private-passphrase",
            "name": "Avery Quinn",
            "weekly_hours": "20",
            "employment_type": "part_time",
            "hired_on": date.today().isoformat(),
        },
        follow_redirects=True,
    )
    c = appmod.db()
    row = c.execute("SELECT id FROM users WHERE username='avery'").fetchone()
    assert row, "new employee was not added"
    c.close()
    login("avery", "avery-private-passphrase")
    avery_ids = shift_ids(house_of("avery"))
    assert len(avery_ids) == 21, f"expected 21 shifts in avery's house, got {len(avery_ids)}"
    form = {f"rank_{sid}": str(i + 1) for i, sid in enumerate(avery_ids)}
    r = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved" in r.data, "avery's 21-shift pick was rejected"
    print("  avery picked all 21 shifts: accepted")

    # ---------- 11. hours-cap stress ----------
    ph(11, "hours-cap stress: riley capped at 16h ranks 5 days")
    login("riley")
    c = appmod.db()
    orig_cap = c.execute("SELECT weekly_hours FROM users WHERE username='riley'").fetchone()[
        "weekly_hours"
    ]
    c.execute("UPDATE users SET weekly_hours=16 WHERE username='riley'")
    c.commit()
    after_id = c.execute("SELECT MAX(id) AS m FROM notifications").fetchone()["m"] or 0
    c.close()
    form = full_rank_form(day_shift_ids(house_of("riley")), ["Mon", "Tue", "Wed", "Fri", "Sat"])
    r = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved" in r.data, "riley cap-stress pick rejected"
    # Finish the debounced rebuild before checking hours caps and notifications.
    appmod.run_scheduler(WEEK)
    hrs = assigned_hours("riley")
    assert hrs <= 16.01, f"riley over hours cap: {hrs:.1f}h > 16h"
    assert conflict_note_matching("riley", "%over your 16h weekly cap%", after_id), (
        "riley got no 16h-cap conflict notification for the over-pick"
    )
    c = appmod.db()
    c.execute("UPDATE users SET weekly_hours=? WHERE username='riley'", (orig_cap,))
    c.commit()
    c.close()
    print(f"  riley held to {hrs:.0f}h/16h with conflict notice; cap restored to {orig_cap}h")

    # ---------- 12. days-off stress ----------
    ph(12, "days-off stress: sam gets exclusive house access, ranks 6 days")
    # For the 2-days-off rule to bind, sam must actually win shifts on 6+
    # distinct days. Under real roster competition he only wins 3, so the
    # rule never fires (false confidence). Quarantine: stash competitors'
    # picks in sam's house and give sam a high cap so only the days-off
    # rule can bind. Everything is restored afterwards.
    sam_house = house_of("sam")
    sam_uid = uid("sam")
    c = appmod.db()
    orig_cap = c.execute("SELECT weekly_hours FROM users WHERE username='sam'").fetchone()[
        "weekly_hours"
    ]
    comp_picks = c.execute(
        "SELECT p.user_id, p.shift_id, p.rank FROM picks p "
        "JOIN users u ON u.id=p.user_id "
        "JOIN shifts s ON s.id=p.shift_id "
        "WHERE u.username!='sam' AND s.area=? AND s.week_start=?",
        (sam_house, WEEK),
    ).fetchall()
    c.execute(
        "DELETE FROM picks WHERE user_id != ? AND shift_id IN "
        "(SELECT id FROM shifts WHERE area=? AND week_start=?)",
        (sam_uid, sam_house, WEEK),
    )
    # Cap must exceed the most sam could ever take unopposed (7 days x 3
    # shifts x 8h = 168h) so the hours rule can never bind before the
    # days-off rule does.
    c.execute("UPDATE users SET weekly_hours=200 WHERE username='sam'")
    c.commit()
    after_id = c.execute("SELECT MAX(id) AS m FROM notifications").fetchone()["m"] or 0
    c.close()
    login("sam")
    form = full_rank_form(day_shift_ids(sam_house), ["Mon", "Tue", "Wed", "Thu", "Fri", "Sun"])
    r = client.post("/pick", data=form, follow_redirects=True)
    assert b"Preferences saved" in r.data, "sam 6-day pick rejected"
    # Finish the debounced rebuild before checking scheduler output.
    appmod.run_scheduler(WEEK)
    days = workdays("sam")
    assert len(days) == 5, f"expected exactly 5 workdays, got {sorted(days)}"
    assert conflict_note_matching("sam", "%days off this week%", after_id), (
        "sam got no days-off conflict notification for the 6-day pick"
    )
    print(f"  sam works {len(days)} days (max 5) with days-off notice")
    # restore competitors' picks and sam's cap, then rebuild the schedule
    c = appmod.db()
    for pk in comp_picks:
        c.execute(
            "INSERT INTO picks (user_id, shift_id, rank) VALUES (?,?,?)",
            (pk["user_id"], pk["shift_id"], pk["rank"]),
        )
    c.execute("UPDATE users SET weekly_hours=? WHERE username='sam'", (orig_cap,))
    c.commit()
    c.close()
    appmod.run_scheduler(WEEK)
    print("  roster restored; schedule rebuilt")

    # ---------- 13. final audit ----------
    ph(13, "final audit")
    sched = schedule()
    for d, n, s, names in sched:
        print(f"    {d}: {n}/{s}  [{names}]")
    overcap = [f"{d} {n}/{s}" for d, n, s, _ in sched if n > s]
    assert not overcap, f"capacity violations in final audit: {overcap}"
    c = appmod.db()
    sick = c.execute(
        "SELECT u.username, s.day FROM assignments a JOIN users u ON "
        "u.id=a.user_id JOIN shifts s ON s.id=a.shift_id WHERE "
        "a.status='sick' AND s.week_start=?",
        (WEEK,),
    ).fetchall()
    over = c.execute(
        "SELECT u.username, SUM(EXTRACT(EPOCH FROM (s2.end_time - s2.start_time)) / 3600.0) h "
        "FROM assignments a JOIN users u ON u.id=a.user_id JOIN shifts s2 ON "
        "s2.id=a.shift_id WHERE a.status NOT IN ('sick','swap_requested') AND s2.week_start=? GROUP BY a.user_id, u.username",
        (WEEK,),
    ).fetchall()
    reqs = c.execute(
        "SELECT kind, status, COUNT(*) n FROM requests GROUP BY kind, status"
    ).fetchall()
    c.close()
    print("  sick rows:", [(r["username"], r["day"]) for r in sick] or "none")
    print("  weekly hours:", {r["username"]: round(r["h"]) for r in over})
    print("  request log:", [(r["kind"], r["status"], r["n"]) for r in reqs])
    print("\nGAUNTLET DONE")


if __name__ == "__main__":
    run_gauntlet()
