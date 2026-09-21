"""DuckDB connections and the paths the layers live at.

Bronze is the gzipped JSON in B2 and is read in place. Silver and gold are
parquet on a local volume, which on the cluster is a Longhorn PVC shared
between the jobs and Jupyter.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import duckdb

DEFAULT_ENDPOINT = "s3.us-west-004.backblazeb2.com"
DEFAULT_REGION = "us-west-004"


def sql_literal(value: object) -> str:
    """Quote a value for interpolation into SQL.

    Paths and credentials go into statements DuckDB will not take parameters
    for, such as COPY targets and CREATE SECRET.
    """
    return "'" + str(value).replace("'", "''") + "'"


def data_dir() -> Path:
    return Path(os.environ.get("HONEYPOT_DATA_DIR", "/data"))


def bronze_prefix() -> str:
    """Root of the shipped logs, without a trailing slash.

    Normally an s3:// URI. Tests point it at a local directory, which DuckDB
    globs the same way.
    """
    return os.environ.get(
        "HONEYPOT_B2_PREFIX", "s3://cowrie-log/cowrie/honeypod1"
    ).rstrip("/")


def bronze_uri(day: dt.date) -> str:
    """Glob matching one day's shipped logs.

    export-logs.sh writes one file per day, but the glob leaves room for a
    second honeypot shipping into the same layout later.
    """
    return (
        f"{bronze_prefix()}/year={day.year:04d}"
        f"/month={day.month:02d}/day={day.day:02d}/*.gz"
    )


def silver_events_dir(day: dt.date) -> Path:
    return (
        data_dir()
        / "silver"
        / "events"
        / f"year={day.year:04d}"
        / f"month={day.month:02d}"
        / f"day={day.day:02d}"
    )


def silver_events_glob() -> str:
    """Every silver partition, as a path glob."""
    return str(data_dir() / "silver" / "events" / "**" / "*.parquet")


def silver_events_source() -> str:
    """SQL expression for the whole silver events dataset.

    Use this rather than hand writing read_parquet: it pins the two things a
    reader gets wrong otherwise.

    union_by_name, because Cowrie's field set differs by event type and so by
    day, and a plain read would fail or drop columns once the days disagree.

    hive_types, because year, month and day come from the directory names and
    DuckDB would otherwise guess per column: month=09 reads as a string while
    day=13 reads as an integer, and the guess for day would flip the first
    time a single digit day lands. All three are strings, always.
    """
    return (
        "read_parquet("
        + sql_literal(silver_events_glob())
        + ", union_by_name = true, hive_partitioning = true,"
        + " hive_types = {'year': VARCHAR, 'month': VARCHAR, 'day': VARCHAR})"
    )


def postgres_dsn() -> str:
    """Where the published session grain lives, for Grafana to read.

    A whole connection string in HONEYPOT_PG_DSN if there is one, which is
    convenient against a throwaway database. Otherwise empty, which hands the
    question to libpq and its PGHOST, PGUSER, PGPASSWORD and PGDATABASE. The
    cluster uses those, so only the password has to come from a secret and the
    rest stays readable in the chart.
    """
    return os.environ.get("HONEYPOT_PG_DSN", "")


def workbench_db() -> Path:
    """The database file the notebooks work against.

    DuckDB allows one writer per file. Jobs deliberately do not open it: they
    read parquet and write parquet, and taking the write lock would mean a
    notebook left open overnight stops the pipeline.
    """
    return data_dir() / "duckdb" / "honeypot.duckdb"


def connect(
    database: str | Path | None = None, read_only: bool = False
) -> duckdb.DuckDBPyConnection:
    """Open a connection with httpfs loaded and B2 configured.

    Defaults to in-memory, which is what a job wants. Pass workbench_db() for
    a session that should keep its views and tables around.

    The B2 secret is only created when credentials are in the environment, so
    a connection that only touches local parquet works without them.
    """
    if database is None:
        con = duckdb.connect()
    else:
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(database), read_only=read_only)

    # Keep extensions on the volume so a fresh pod does not refetch them.
    ext_dir = data_dir() / "duckdb" / "extensions"
    ext_dir.mkdir(parents=True, exist_ok=True)
    con.execute("SET extension_directory = " + sql_literal(ext_dir))

    con.install_extension("httpfs")
    con.load_extension("httpfs")

    key_id = os.environ.get("AWS_ACCESS_KEY_ID")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY")
    if key_id and secret:
        con.execute(
            "CREATE OR REPLACE SECRET b2 ("
            " TYPE s3,"
            f" KEY_ID {sql_literal(key_id)},"
            f" SECRET {sql_literal(secret)},"
            f" ENDPOINT {sql_literal(os.environ.get('HONEYPOT_B2_ENDPOINT', DEFAULT_ENDPOINT))},"
            f" REGION {sql_literal(os.environ.get('HONEYPOT_B2_REGION', DEFAULT_REGION))},"
            " URL_STYLE 'path'"
            ")"
        )

    return con


def init_views(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Create the views the notebooks query by name.

    A SQL client connecting straight to the file sees only what the file
    holds, so the read options that silver_events_source() pins have to be
    baked into a stored view rather than applied per query.
    """
    con.execute(
        "CREATE OR REPLACE VIEW silver_events AS SELECT * FROM "
        + silver_events_source()
    )
    return ["silver_events"]
