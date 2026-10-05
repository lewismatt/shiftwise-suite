"""Configure a retained, isolated PostgreSQL schema before importing the app."""

import os
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import uuid4


def prepare_database(label, database_url=None):
    """Return a fresh schema name and route every app connection to it.

    The target role needs CREATE on the database. No public-schema fallback is
    included: force-seeding a scenario must never reset the regular app tables.
    Schemas remain available after the run for inspection and manual cleanup.
    """
    import psycopg
    from psycopg import sql

    database_url = database_url or os.environ.get("DATABASE_URL", "")
    if not database_url.lower().startswith(("postgres://", "postgresql://")):
        raise ValueError("Set DATABASE_URL (or --database-url) to a PostgreSQL URL.")
    engine = os.environ.get("SHIFTWISE_DB_ENGINE", "postgres").lower()
    if engine not in ("postgres", "postgresql"):
        raise ValueError("SHIFTWISE_DB_ENGINE must be postgres or postgresql.")

    schema = f"shiftwise_{label}_{uuid4().hex}"
    parts = urlsplit(database_url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    options = " ".join(value for key, value in query if key == "options")
    query = [(key, value) for key, value in query if key != "options"]
    query.append(("options", f"{options} -c search_path={schema}".strip()))
    isolated_url = urlunsplit(parts._replace(query=urlencode(query, quote_via=quote)))
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    os.environ["DATABASE_URL"] = isolated_url
    print(f"PostgreSQL scenario schema: {schema} (retained for inspection)")
    return schema
