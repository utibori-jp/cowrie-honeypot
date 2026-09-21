"""Exercises the silver to postgres transform without a postgres.

The transform is the part with decisions in it, so it is a SELECT that can be
run against silver directly. The insert that carries the rows over is thin
enough that a smoke test covers it.
"""

from __future__ import annotations

import datetime as dt
import os

import pytest

from honeypot_analytics import db, normalize, publish
from tests.test_normalize import write_bronze

DAY = dt.date(2026, 9, 15)

# The round trip needs a real postgres. Everything above it does not, so the
# suite still says something useful where one is not running.
needs_pg = pytest.mark.skipif(
    not os.environ.get("HONEYPOT_PG_DSN"),
    reason="set HONEYPOT_PG_DSN to a throwaway postgres to run this",
)


def event(eventid, session="s1", ts="2026-09-15 01:02:03.000000", **extra):
    return {
        "eventid": eventid,
        "session": session,
        "src_ip": "1.2.3.4",
        "timestamp": ts,
        **extra,
    }


def silver(env, records, day=DAY):
    write_bronze(env / "bronze", day, records)
    con = db.connect()
    normalize.normalize_day(con, day)
    return con


def test_selects_cover_only_the_requested_day(env):
    # The day scoped delete and insert is only safe if the select stops at the
    # partition boundary.
    other = dt.date(2026, 9, 16)
    write_bronze(env / "bronze", DAY, [event("cowrie.session.connect")])
    write_bronze(
        env / "bronze",
        other,
        [
            event(
                "cowrie.session.connect",
                session="s2",
                ts="2026-09-16 01:02:03.000000",
            )
        ],
    )
    con = db.connect()
    normalize.normalize_day(con, DAY)
    normalize.normalize_day(con, other)

    assert [
        r[0] for r in con.execute(publish.sessions_select(DAY)).fetchall()
    ] == ["s1"]
    assert [
        r[0] for r in con.execute(publish.sessions_select(other)).fetchall()
    ] == ["s2"]


def test_commands_keep_one_row_per_input(env):
    con = silver(
        env,
        [
            event("cowrie.session.connect"),
            event(
                "cowrie.command.input",
                ts="2026-09-15 01:02:04.000000",
                input="uname -a",
            ),
            event(
                "cowrie.command.input",
                ts="2026-09-15 01:02:05.000000",
                input="whoami",
            ),
        ],
    )

    rows = con.execute(publish.commands_select(DAY)).fetchall()

    assert rows == [
        ("s1", DAY, dt.datetime(2026, 9, 15, 1, 2, 4), "uname -a"),
        ("s1", DAY, dt.datetime(2026, 9, 15, 1, 2, 5), "whoami"),
    ]


def test_login_attempts_keep_failures_alongside_successes(env):
    con = silver(
        env,
        [
            event("cowrie.session.connect"),
            event(
                "cowrie.login.failed",
                ts="2026-09-15 01:02:04.000000",
                username="root",
                password="wrong",
            ),
            event(
                "cowrie.login.success",
                ts="2026-09-15 01:02:05.000000",
                username="root",
                password="123qwe",
            ),
        ],
    )

    rows = con.execute(publish.login_attempts_select(DAY)).fetchall()

    assert rows == [
        ("s1", DAY, dt.datetime(2026, 9, 15, 1, 2, 4), "root", "wrong", False),
        ("s1", DAY, dt.datetime(2026, 9, 15, 1, 2, 5), "root", "123qwe", True),
    ]


def test_session_without_a_connect_in_this_partition_is_skipped(env):
    # A session open when the log rotates leaves its close in the next day's
    # partition. That tail has no connect, so it has no connect_time, and the
    # row it would produce cannot be stored.
    con = silver(
        env,
        [
            event("cowrie.command.input", input="whoami"),
            event("cowrie.session.closed", duration_ms=4500),
        ],
    )

    assert con.execute(publish.sessions_select(DAY)).fetchall() == []


def test_session_row_carries_the_derived_fields(env):
    con = silver(
        env,
        [
            event("cowrie.session.connect", ts="2026-09-15 01:02:03.000000"),
            event("cowrie.client.version", version="SSH-2.0-OpenSSH_9.9"),
            event("cowrie.client.kex", hassh="abc123"),
            event("cowrie.login.success", username="root", password="123qwe"),
            event("cowrie.command.input", input="uname -a"),
            event("cowrie.command.input", input="whoami"),
            event("cowrie.session.closed", duration_ms=4500),
        ],
    )

    rows = con.execute(publish.sessions_select(DAY)).fetchall()

    assert rows == [
        (
            "s1",
            DAY,
            dt.datetime(2026, 9, 15, 1, 2, 3),
            "1.2.3.4",
            4500,
            True,
            2,
            "SSH-2.0-OpenSSH_9.9",
            "abc123",
        )
    ]


@needs_pg
def test_publish_day_loads_the_three_tables(env):
    con = silver(
        env,
        [
            event("cowrie.session.connect"),
            event("cowrie.login.success", username="root", password="123qwe"),
            event("cowrie.command.input", input="uname -a"),
            event("cowrie.session.closed", duration_ms=4500),
        ],
    )
    publish.create_schema(con)

    assert publish.publish_day(con, DAY) == {
        "sessions": 1,
        "login_attempts": 1,
        "commands": 1,
    }
    assert con.execute(
        "SELECT session, command_count FROM pg.sessions"
    ).fetchall() == [("s1", 1)]


@needs_pg
def test_publishing_a_day_twice_replaces_rather_than_duplicates(env):
    con = silver(
        env,
        [
            event("cowrie.session.connect"),
            event("cowrie.command.input", input="uname -a"),
        ],
    )
    publish.create_schema(con)

    publish.publish_day(con, DAY)
    publish.publish_day(con, DAY)

    assert con.execute("SELECT count(*) FROM pg.sessions").fetchone() == (1,)
    assert con.execute("SELECT count(*) FROM pg.commands").fetchone() == (1,)


def test_publish_takes_the_same_date_arguments_as_normalize():
    from honeypot_analytics import cli

    args = cli.build_parser().parse_args(
        ["publish", "--from", "2026-09-13", "--to", "2026-09-15"]
    )
    assert cli._resolve_range(args) == (
        dt.date(2026, 9, 13),
        dt.date(2026, 9, 15),
    )

    empty = cli.build_parser().parse_args(["publish", "--date", ""])
    assert cli._resolve_range(empty) == (cli._yesterday(), cli._yesterday())


@needs_pg
def test_libpq_environment_variables_work_without_a_dsn(env, monkeypatch):
    # What the cluster uses: host, user and database sit in the chart as plain
    # values and only the password comes from a secret.
    dsn = os.environ["HONEYPOT_PG_DSN"]
    rest, _, database = dsn.rpartition("/")
    creds, _, hostport = rest.rpartition("@")
    user, _, password = creds.replace("postgresql://", "").partition(":")
    host, _, port = hostport.partition(":")

    monkeypatch.delenv("HONEYPOT_PG_DSN")
    monkeypatch.setenv("PGHOST", host)
    monkeypatch.setenv("PGPORT", port)
    monkeypatch.setenv("PGUSER", user)
    monkeypatch.setenv("PGPASSWORD", password)
    monkeypatch.setenv("PGDATABASE", database)

    con = silver(env, [event("cowrie.session.connect")])
    publish.create_schema(con)
    assert publish.publish_day(con, DAY)["sessions"] == 1


def test_publishing_a_day_with_no_silver_is_an_error(env):
    # Without this, the delete lands and the insert brings nothing, so a typo
    # in a manual --date silently empties that day in postgres.
    con = db.connect()
    with pytest.raises(publish.NoSilverForDay):
        publish.publish_day(con, DAY)


@needs_pg
def test_allow_missing_leaves_what_is_already_there(env):
    con = silver(env, [event("cowrie.session.connect")])
    publish.create_schema(con)
    publish.publish_day(con, DAY)

    other = dt.date(2026, 9, 16)
    assert publish.publish_day(con, other, allow_missing=True) == {
        "sessions": 0,
        "login_attempts": 0,
        "commands": 0,
    }
    assert con.execute("SELECT count(*) FROM pg.sessions").fetchone() == (1,)
