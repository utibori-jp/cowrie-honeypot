"""Bronze to silver: the day's JSON lines, written back out as parquet.

No modelling happens here. Whatever columns Cowrie emitted that day are the
columns that get written, so this step never needs revisiting when the schema
questions further down the pipeline get answered differently.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

import duckdb

from .db import bronze_uri, silver_events_dir, sql_literal

log = logging.getLogger(__name__)


class NoDataForDay(Exception):
    """Nothing matched the day's glob in B2."""


def _source_files(con: duckdb.DuckDBPyConnection, uri: str) -> list[str]:
    rows = con.execute(
        "SELECT file FROM glob(" + sql_literal(uri) + ") ORDER BY file"
    ).fetchall()
    return [r[0] for r in rows]


def normalize_day(
    con: duckdb.DuckDBPyConnection,
    day: dt.date,
    allow_missing: bool = False,
) -> int:
    """Rewrite one day's partition. Returns the row count written.

    Replacing a whole partition is what makes this safe to re-run: a repeat
    produces the same file, and a backfill is a loop over days.
    """
    uri = bronze_uri(day)
    files = _source_files(con, uri)
    if not files:
        if allow_missing:
            log.warning("no source files for %s, skipping", day)
            return 0
        raise NoDataForDay(
            f"no files matched {uri}. The honeypot ships a day's log once it "
            f"has rotated, so a recent date may simply not be there yet. Pass "
            f"--allow-missing to treat this as a skip."
        )

    log.info("%s: reading %d file(s)", day, len(files))

    out_dir = silver_events_dir(day)
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / "events.parquet"
    # Same directory as the destination, so the replace below is atomic and a
    # crashed job cannot leave a half written partition behind.
    tmp = out_dir / ".events.parquet.tmp"

    con.execute(
        "COPY (SELECT * FROM read_json_auto("
        + sql_literal(uri)
        + ", union_by_name = true, ignore_errors = true,"
        # Infer the schema from every line, not a sample. Rare event types
        # carry fields nothing else does, and at the default sample size
        # DuckDB never sees them: a day with 34k events silently lost three
        # columns that way.
        " sample_size = -1,"
        # Cowrie emits a filename field of its own on download events, so the
        # provenance column cannot use that name.
        " filename = 'source_file')) TO "
        + sql_literal(tmp)
        + " (FORMAT parquet, COMPRESSION zstd)"
    )

    rows = con.execute(
        "SELECT count(*) FROM read_parquet(" + sql_literal(tmp) + ")"
    ).fetchone()[0]

    os.replace(tmp, final)
    log.info("%s: wrote %d rows to %s", day, rows, final)
    return rows


def normalize_range(
    con: duckdb.DuckDBPyConnection,
    start: dt.date,
    end: dt.date,
    allow_missing: bool = False,
) -> dict[dt.date, int]:
    results: dict[dt.date, int] = {}
    day = start
    while day <= end:
        results[day] = normalize_day(con, day, allow_missing=allow_missing)
        day += dt.timedelta(days=1)
    return results
