"""Calendar view and container health check routes."""

from collections import defaultdict
from datetime import date, timedelta

from flask import (
    Blueprint,
    g,
    render_template,
    request,
    session,
)

from shiftwise.auth import login_required
from shiftwise.db import db, monday_of
from shiftwise.domain.constants import DAYS

calendar_bp = Blueprint("calendar", __name__)


@calendar_bp.route("/healthz")
def healthz():
    try:
        conn = db()
        # Require initialized application tables, not just a reachable server.
        conn.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        conn.close()
        return {"status": "ok"}, 200
    except Exception:
        # never leak internal error details to unauthenticated callers
        return {"status": "error"}, 503


def calendar_days(conn, week, user_id, area=None):
    """7-day grid for a week: shifts per day, assignment status + staff for user_id."""
    if area in ("front", "back"):
        shifts = conn.execute(
            "SELECT * FROM shifts WHERE week_start=? AND area=? ORDER BY id", (week, area)
        ).fetchall()
    else:
        shifts = conn.execute(
            "SELECT * FROM shifts WHERE week_start=? ORDER BY area, id", (week,)
        ).fetchall()
    staff = defaultdict(list)
    for row in conn.execute(
        "SELECT a.shift_id, a.status, a.user_id, u.name FROM assignments a "
        "JOIN users u ON u.id=a.user_id JOIN shifts s ON s.id=a.shift_id "
        "WHERE s.week_start=? AND a.status NOT IN ('sick','swap_requested') ORDER BY a.id",
        (week,),
    ):
        staff[row["shift_id"]].append(row)
    by_day = defaultdict(list)
    for s in shifts:
        rows = staff[s["id"]]
        status = next((r["status"] for r in rows if r["user_id"] == user_id), None)
        by_day[s["day"]].append(
            {
                "id": s["id"],
                "time": f"{s['start_time']}–{s['end_time']}",
                "start_time": s["start_time"],
                "end_time": s["end_time"],
                "note": s["note"],
                "area": s["area"],
                "status": status,
                "mine": status is not None,
                "who": ", ".join(r["name"] for r in rows) or None,
            }
        )
    start = date.fromisoformat(week)
    today = date.today().isoformat()
    return [
        {
            "name": DAYS[i],
            "date": (start + timedelta(days=i)).isoformat(),
            "label": (start + timedelta(days=i)).strftime("%b %d"),
            "shifts": by_day[DAYS[i]],
            "today": (start + timedelta(days=i)).isoformat() == today,
        }
        for i in range(7)
    ]


@calendar_bp.route("/calendar")
@login_required()
def calendar_view():
    raw = request.args.get("week", "")
    try:
        week = monday_of(date.fromisoformat(raw)).isoformat()
    except ValueError:
        week = monday_of(date.today()).isoformat()
    viewer = g.current_user
    uid = viewer["id"]
    area = request.args.get("area")
    if area not in ("front", "back", "all"):
        station = viewer["station"]
        area = station if station in ("front", "back") else "all" if station == "both" else "front"
    conn = db()
    employees = []
    view_user = viewer
    if session["role"] == "manager":
        if area == "all":
            employees = conn.execute(
                "SELECT id, name FROM users WHERE role='employee' ORDER BY name"
            ).fetchall()
        else:
            employees = conn.execute(
                "SELECT id, name FROM users WHERE role='employee' "
                "AND station IN (?, 'both') ORDER BY name",
                (area,),
            ).fetchall()
        req = request.args.get("user_id", "")
        if req.isdigit():
            selected = next((row for row in employees if row["id"] == int(req)), None)
            if selected:
                uid = selected["id"]
                view_user = selected
    days = calendar_days(conn, week, uid, area)
    preference_weeks = [monday_of(date.today()).isoformat()]
    preference_weeks.append(
        (date.fromisoformat(preference_weeks[0]) + timedelta(days=7)).isoformat()
    )
    preference_shifts = []
    preference_picks = {}
    cover_prefs = {}
    if session["role"] == "employee":
        station = viewer["station"]
        areas = ("front", "back") if station == "both" else (station,)
        placeholders = ",".join("?" for _ in areas)
        week_placeholders = ",".join("?" for _ in preference_weeks)
        preference_shifts = conn.execute(
            f"SELECT * FROM shifts WHERE week_start IN ({week_placeholders}) "
            f"AND area IN ({placeholders}) ORDER BY week_start, id",
            (*preference_weeks, *areas),
        ).fetchall()
        preference_picks = {
            row["shift_id"]: row["rank"]
            for row in conn.execute(
                "SELECT p.shift_id, p.rank FROM picks p JOIN shifts s ON s.id=p.shift_id "
                f"WHERE p.user_id=? AND s.week_start IN ({week_placeholders})",
                (session["uid"], *preference_weeks),
            )
        }
        cover_prefs = {
            row["shift_id"]: row["willing"]
            for row in conn.execute(
                "SELECT cp.shift_id, cp.willing FROM coverage_preferences cp "
                "JOIN shifts s ON s.id=cp.shift_id "
                f"WHERE cp.user_id=? AND s.week_start IN ({week_placeholders})",
                (session["uid"], *preference_weeks),
            )
        }
    conn.close()
    d = date.fromisoformat(week)
    return render_template(
        "calendar.html",
        days=days,
        DAYS=DAYS,
        preference_weeks=preference_weeks,
        preference_shifts=preference_shifts,
        preference_picks=preference_picks,
        cover_prefs=cover_prefs,
        week=week,
        view_name=view_user["name"] if view_user else session.get("name", ""),
        employees=employees,
        view_id=uid,
        area=area,
        prev_week=(d - timedelta(days=7)).isoformat(),
        next_week=(d + timedelta(days=7)).isoformat(),
        this_week=monday_of(date.today()).isoformat(),
    )
