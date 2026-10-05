"""Scenario seeding must leave regular application data intact."""

import os
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import sql

import app as appmod
from tools.scenario_database import prepare_database


def test_scenario_schema_preserves_options_and_regular_roster():
    original_url = os.environ["DATABASE_URL"]
    conn = appmod.db()
    try:
        conn.execute(
            "INSERT INTO users (username, password, name) VALUES (?, ?, ?)",
            ("regular-worker", "hash", "Regular Worker"),
        )
        conn.commit()
    finally:
        conn.close()

    parts = urlsplit(original_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    options = " ".join(value for key, value in query if key == "options")
    query = [(key, value) for key, value in query if key != "options"]
    query.append(("options", f"{options} -c application_name=shiftwise-isolation-test".strip()))
    scenario_url = urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
    schema = prepare_database("test", scenario_url)
    try:
        appmod.init_db(seed_demo=True)
        conn = appmod.db()
        try:
            assert conn.execute("SELECT current_schema()").fetchone()[0] == schema
            assert conn.execute("SHOW application_name").fetchone()[0] == "shiftwise-isolation-test"
            assert (
                conn.execute("SELECT 1 FROM users WHERE username='regular-worker'").fetchone()
                is None
            )
            assert conn.execute("SELECT 1 FROM users WHERE username='manager'").fetchone()[0] == 1
        finally:
            conn.close()
        with psycopg.connect(original_url) as original:
            assert original.execute("SELECT username FROM users").fetchall() == [
                ("regular-worker",)
            ]
    finally:
        os.environ["DATABASE_URL"] = original_url
        with psycopg.connect(original_url, autocommit=True) as original:
            original.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
