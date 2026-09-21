"""Silver to postgres, at session grain.

The transform is a SELECT per table so it can be run against silver on its own,
which is what the tests do. Carrying the rows over is a separate step that needs
a database.
"""

from __future__ import annotations

import datetime as dt
import logging

import duckdb

from .db import (
    postgres_dsn,
    silver_events_dir,
    silver_events_source,
    sql_literal,
)

log = logging.getLogger(__name__)


class NoSilverForDay(Exception):
    """Nothing normalized for that day yet."""

# Which select fills which table, and under which columns. The identity keys on
# the two detail tables are left to postgres, so every insert names its columns.
TABLES = {
    "sessions": (
        "session, day, connect_time, src_ip, duration_ms,"
        " login_success, command_count, client_version, hassh"
    ),
    "login_attempts": "session, day, ts, username, password, success",
    "commands": "session, day, ts, input",
}

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session        text PRIMARY KEY,
        day            date NOT NULL,
        connect_time   timestamptz NOT NULL,
        src_ip         inet NOT NULL,
        duration_ms    bigint,
        login_success  boolean NOT NULL,
        command_count  integer NOT NULL,
        client_version text,
        hassh          text
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS login_attempts (
        id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        session  text NOT NULL,
        day      date NOT NULL,
        ts       timestamptz NOT NULL,
        username text,
        password text,
        success  boolean NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS commands (
        id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        session text NOT NULL,
        day     date NOT NULL,
        ts      timestamptz NOT NULL,
        input   text NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS sessions_day ON sessions (day)",
    "CREATE INDEX IF NOT EXISTS sessions_connect_time"
    " ON sessions (connect_time)",
    "CREATE INDEX IF NOT EXISTS sessions_src_ip ON sessions (src_ip)",
    "CREATE INDEX IF NOT EXISTS login_attempts_day ON login_attempts (day)",
    "CREATE INDEX IF NOT EXISTS login_attempts_username"
    " ON login_attempts (username)",
    "CREATE INDEX IF NOT EXISTS login_attempts_password"
    " ON login_attempts (password)",
    "CREATE INDEX IF NOT EXISTS commands_day ON commands (day)",
]


def _day_filter(day: dt.date) -> str:
    """Match one partition by its hive columns, so DuckDB skips the rest."""
    return (
        f"year = {sql_literal(f'{day.year:04d}')}"
        f" AND month = {sql_literal(f'{day.month:02d}')}"
        f" AND day = {sql_literal(f'{day.day:02d}')}"
    )


# Carried by one event type each, so a day that saw none of that event has no
# such column and a plain reference to it fails to bind.
OPTIONAL_COLUMNS = {
    "duration_ms": "BIGINT",
    "version": "VARCHAR",
    "hassh": "VARCHAR",
    "input": "VARCHAR",
    "username": "VARCHAR",
    "password": "VARCHAR",
}


def _silver(day: dt.date) -> str:
    """One day of silver, with the optional columns guaranteed to bind.

    The second branch contributes no rows. It is there so UNION ALL BY NAME
    puts the columns into the result schema whether the day carried them or not.
    """
    empty = ", ".join(
        f"CAST(NULL AS {typ}) AS {name}"
        for name, typ in OPTIONAL_COLUMNS.items()
    )
    return (
        "(SELECT * FROM "
        + silver_events_source()
        + f" WHERE {_day_filter(day)}"
        " UNION ALL BY NAME"
        f" SELECT {empty} WHERE FALSE)"
    )


def sessions_select(day: dt.date) -> str:
    # The day is emitted as a literal rather than rebuilt from the partition
    # columns, which would collide with silver's own `day`.
    return (
        "SELECT"
        "  session,"
        f"  CAST({sql_literal(day.isoformat())} AS DATE) AS day,"
        "  min(timestamp) FILTER (eventid = 'cowrie.session.connect')"
        "    AS connect_time,"
        "  any_value(src_ip) FILTER (eventid = 'cowrie.session.connect')"
        "    AS src_ip,"
        "  max(duration_ms) FILTER (eventid = 'cowrie.session.closed')"
        "    AS duration_ms,"
        "  count(*) FILTER (eventid = 'cowrie.login.success') > 0"
        "    AS login_success,"
        "  count(*) FILTER (eventid = 'cowrie.command.input')"
        "    AS command_count,"
        "  any_value(version) FILTER (eventid = 'cowrie.client.version')"
        "    AS client_version,"
        "  any_value(hassh) FILTER (eventid = 'cowrie.client.kex')"
        "    AS hassh"
        f" FROM {_silver(day)}"
        " GROUP BY session"
        " HAVING count(*) FILTER (eventid = 'cowrie.session.connect') > 0"
    )


def login_attempts_select(day: dt.date) -> str:
    return (
        "SELECT"
        "  session,"
        f"  CAST({sql_literal(day.isoformat())} AS DATE) AS day,"
        "  timestamp AS ts,"
        "  username,"
        "  password,"
        "  eventid = 'cowrie.login.success' AS success"
        f" FROM {_silver(day)}"
        " WHERE eventid IN ('cowrie.login.success', 'cowrie.login.failed')"
        " ORDER BY timestamp"
    )


def commands_select(day: dt.date) -> str:
    return (
        "SELECT"
        "  session,"
        f"  CAST({sql_literal(day.isoformat())} AS DATE) AS day,"
        "  timestamp AS ts,"
        "  input"
        f" FROM {_silver(day)}"
        " WHERE eventid = 'cowrie.command.input'"
        " ORDER BY timestamp"
    )


def attach(con: duckdb.DuckDBPyConnection) -> None:
    """Make the postgres database available as `pg`."""
    con.install_extension("postgres")
    con.load_extension("postgres")
    con.execute(
        "ATTACH IF NOT EXISTS "
        + sql_literal(postgres_dsn())
        + " AS pg (TYPE postgres)"
    )


def create_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the tables Grafana reads, if they are not there already.

    Through postgres_execute rather than the attached database, because the
    extension cannot carry constraints across an ordinary CREATE TABLE and the
    primary keys and NOT NULLs are the point.
    """
    attach(con)
    for statement in SCHEMA:
        con.execute("CALL postgres_execute(?, ?)", ["pg", statement])


def publish_day(
    con: duckdb.DuckDBPyConnection, day: dt.date, allow_missing: bool = False
) -> dict[str, int]:
    """Replace one day in postgres. Returns the row count per table.

    Delete then insert inside one transaction, so a re-run lands the same rows
    and a failure part way through leaves the day as it was.
    """
    if not (silver_events_dir(day) / "events.parquet").exists():
        if allow_missing:
            log.warning("no silver for %s, leaving postgres alone", day)
            return dict.fromkeys(TABLES, 0)
        raise NoSilverForDay(
            f"{day} has not been normalized. Publishing it anyway would "
            f"delete the day from postgres and put nothing back. Run normalize "
            f"for that day first, or pass --allow-missing to skip it."
        )

    attach(con)
    selects = {
        "sessions": sessions_select(day),
        "login_attempts": login_attempts_select(day),
        "commands": commands_select(day),
    }
    literal_day = f"CAST({sql_literal(day.isoformat())} AS DATE)"

    con.execute("BEGIN")
    try:
        for table, columns in TABLES.items():
            con.execute(f"DELETE FROM pg.{table} WHERE day = {literal_day}")
            con.execute(
                f"INSERT INTO pg.{table} ({columns}) {selects[table]}"
            )
        counts = {
            table: con.execute(
                f"SELECT count(*) FROM pg.{table} WHERE day = {literal_day}"
            ).fetchone()[0]
            for table in TABLES
        }
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    log.info("%s: published %s", day, counts)
    return counts


def publish_range(
    con: duckdb.DuckDBPyConnection,
    start: dt.date,
    end: dt.date,
    allow_missing: bool = False,
) -> dict[dt.date, dict[str, int]]:
    results: dict[dt.date, dict[str, int]] = {}
    day = start
    while day <= end:
        results[day] = publish_day(con, day, allow_missing=allow_missing)
        day += dt.timedelta(days=1)
    return results
