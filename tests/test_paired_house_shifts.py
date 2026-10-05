"""A posted FOH shift must have a matching BOH shift, and vice versa."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from test_support import truncate_postgres_tables

import app as appmod


def test_paired_house_shifts():
    def fresh():
        trunc_conn = appmod.db()
        try:
            truncate_postgres_tables(trunc_conn)
        finally:
            trunc_conn.close()
        appmod.init_db(seed_demo=True)
        conn = appmod.db()
        conn.execute("DELETE FROM shifts")
        conn.commit()
        conn.close()

    def manager_client():
        client = appmod.app.test_client()
        client.post("/login", data={"username": "manager", "password": "manager"})
        return client

    fresh()
    response = manager_client().post(
        "/manager/shift/add",
        data={
            "area": "front",
            "day": "Mon",
            "start": "09:00",
            "end": "17:00",
            "slots": "2",
            "note": "opening",
        },
    )
    assert response.status_code == 302
    conn = appmod.db()
    rows = conn.execute(
        "SELECT area, day, start_time, end_time, slots, note FROM shifts ORDER BY area"
    ).fetchall()
    conn.close()
    assert [
        (r["area"], r["day"], r["start_time"], r["end_time"], r["slots"], r["note"]) for r in rows
    ] == [
        ("back", "Mon", "09:00", "17:00", 2, "opening"),
        ("front", "Mon", "09:00", "17:00", 2, "opening"),
    ], "posting a FOH shift must create its matching BOH shift"
    print("Posting FOH creates matching FOH and BOH shifts: OK")


if __name__ == "__main__":
    test_paired_house_shifts()
