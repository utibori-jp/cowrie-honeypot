"""Exercises the real normalize path against local gzipped fixtures.

DuckDB globs a local directory the same way it globs s3, so pointing
HONEYPOT_B2_PREFIX at a temp dir covers the SQL without touching B2.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json

import pytest

from honeypot_analytics import db, normalize

DAY = dt.date(2026, 9, 15)


def write_bronze(prefix, day, records):
    part = (
        prefix
        / f"year={day.year:04d}"
        / f"month={day.month:02d}"
        / f"day={day.day:02d}"
    )
    part.mkdir(parents=True)
    path = part / f"cowrie.json.{day.isoformat()}.gz"
    with gzip.open(path, "wt") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HONEYPOT_B2_PREFIX", str(tmp_path / "bronze"))
    monkeypatch.setenv("HONEYPOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    return tmp_path


def test_writes_one_partition(env):
    write_bronze(
        env / "bronze",
        DAY,
        [
            {"eventid": "cowrie.session.connect", "src_ip": "1.2.3.4"},
            {"eventid": "cowrie.command.input", "input": "uname -a"},
        ],
    )
    con = db.connect()
    assert normalize.normalize_day(con, DAY) == 2
    assert (db.silver_events_dir(DAY) / "events.parquet").exists()


def test_keeps_columns_that_only_some_events_have(env):
    write_bronze(
        env / "bronze",
        DAY,
        [
            {"eventid": "cowrie.session.connect", "src_ip": "1.2.3.4"},
            {"eventid": "cowrie.session.file_download", "filename": "x.sh"},
        ],
    )
    con = db.connect()
    normalize.normalize_day(con, DAY)
    cols = {
        r[0]
        for r in con.execute(
            "DESCRIBE SELECT * FROM read_parquet("
            + db.sql_literal(db.silver_events_dir(DAY) / "events.parquet")
            + ")"
        ).fetchall()
    }
    # Cowrie's own filename survives, and the provenance column sits beside it.
    assert {"eventid", "src_ip", "filename", "source_file"} <= cols


def test_rerunning_replaces_rather_than_appends(env):
    write_bronze(
        env / "bronze", DAY, [{"eventid": "cowrie.session.connect"}]
    )
    con = db.connect()
    assert normalize.normalize_day(con, DAY) == 1
    assert normalize.normalize_day(con, DAY) == 1


def test_missing_day_is_an_error_unless_allowed(env):
    con = db.connect()
    with pytest.raises(normalize.NoDataForDay):
        normalize.normalize_day(con, DAY)
    assert normalize.normalize_day(con, DAY, allow_missing=True) == 0


def test_partition_columns_are_all_strings(env):
    # A single digit day next to a two digit one is what makes DuckDB's own
    # guess inconsistent, so the fixture covers both.
    for day in (dt.date(2026, 9, 5), dt.date(2026, 9, 15)):
        write_bronze(env / "bronze", day, [{"eventid": "cowrie.session.connect"}])
    con = db.connect()
    for day in (dt.date(2026, 9, 5), dt.date(2026, 9, 15)):
        normalize.normalize_day(con, day)

    types = {
        name: typ
        for name, typ, *_ in con.execute(
            "DESCRIBE SELECT * FROM " + db.silver_events_source()
        ).fetchall()
    }
    assert types["year"] == "VARCHAR"
    assert types["month"] == "VARCHAR"
    assert types["day"] == "VARCHAR"

    assert con.execute(
        "SELECT day FROM " + db.silver_events_source() + " ORDER BY day"
    ).fetchall() == [("05",), ("15",)]


def test_keeps_fields_that_only_appear_past_the_inference_sample(env):
    # DuckDB samples the first rows to infer a schema unless told otherwise.
    # Cowrie's rare events sit at the end of a busy day, so a field carried by
    # a handful of records has to survive being nowhere near the front.
    common = [{"eventid": "cowrie.session.connect", "src_ip": "1.2.3.4"}] * 25000
    rare = [{"eventid": "cowrie.session.file_download", "url": "http://x/y.sh"}]
    write_bronze(env / "bronze", DAY, common + rare)

    con = db.connect()
    normalize.normalize_day(con, DAY)
    cols = {
        r[0]
        for r in con.execute(
            "DESCRIBE SELECT * FROM " + db.silver_events_source()
        ).fetchall()
    }
    assert "url" in cols
