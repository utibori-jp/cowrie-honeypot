"""Shared fixtures.

Each test gets its own HONEYPOT_DATA_DIR so nothing leaks between them, and
db.connect() keeps its extensions under that directory. Left alone that means
every test downloads httpfs and postgres again, which costs more than the tests
themselves. The cache below fetches them once per session and hands each test a
copy.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil

import pytest

import duckdb


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "needs_pg: writes to postgres, so it needs one to write to"
    )


def pytest_collection_modifyitems(config, items):
    """Skip the postgres tests where there is no postgres.

    Everything else covers the transform on its own, so the suite still says
    something useful without a database. CI runs in exactly that state.
    """
    if os.environ.get("HONEYPOT_PG_DSN"):
        return
    skip = pytest.mark.skip(
        reason="set HONEYPOT_PG_DSN to a throwaway postgres to run this"
    )
    for item in items:
        if "needs_pg" in item.keywords:
            item.add_marker(skip)


def _write_bronze(prefix, day, records):
    part = (
        prefix
        / f"year={day.year:04d}"
        / f"month={day.month:02d}"
        / f"day={day.day:02d}"
    )
    part.mkdir(parents=True, exist_ok=True)
    path = part / f"cowrie.json.{day.isoformat()}.gz"
    with gzip.open(path, "wt") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return path


@pytest.fixture
def write_bronze():
    """Lay down one day of gzipped JSON where the bronze glob will find it.

    A fixture rather than an import, so the two test modules do not have to
    reach into each other. pytest puts conftest fixtures in scope everywhere
    below this directory; a cross module import only works by accident of
    sys.path and breaks under a different working directory.
    """
    return _write_bronze


@pytest.fixture(scope="session")
def extension_cache(tmp_path_factory):
    cache = tmp_path_factory.mktemp("duckdb-extensions")
    con = duckdb.connect()
    con.execute("SET extension_directory = '" + str(cache).replace("\\", "/") + "'")
    for name in ("httpfs", "postgres"):
        con.install_extension(name)
    con.close()
    return cache


@pytest.fixture(autouse=True)
def clean_postgres(request, extension_cache):
    """Empty the published tables before a test that writes to them.

    The tests share one database, and session is the primary key, so a session
    id reused by another test collides no matter which day it belongs to.
    Absolute row counts only mean something on an empty table anyway.
    """
    if "needs_pg" not in request.keywords:
        return
    if not os.environ.get("HONEYPOT_PG_DSN"):
        return

    from honeypot_analytics import publish

    con = duckdb.connect()
    con.execute(
        "SET extension_directory = '"
        + str(extension_cache).replace("\\", "/")
        + "'"
    )
    publish.create_schema(con)
    for table in ("sessions", "login_attempts", "commands"):
        con.execute(f"DELETE FROM pg.{table}")
    con.close()


@pytest.fixture
def env(tmp_path, monkeypatch, extension_cache):
    data = tmp_path / "data"
    shutil.copytree(extension_cache, data / "duckdb" / "extensions")
    monkeypatch.setenv("HONEYPOT_B2_PREFIX", str(tmp_path / "bronze"))
    monkeypatch.setenv("HONEYPOT_DATA_DIR", str(data))
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    return tmp_path
