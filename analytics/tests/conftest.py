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
import shutil

import pytest

import duckdb


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


@pytest.fixture
def env(tmp_path, monkeypatch, extension_cache):
    data = tmp_path / "data"
    shutil.copytree(extension_cache, data / "duckdb" / "extensions")
    monkeypatch.setenv("HONEYPOT_B2_PREFIX", str(tmp_path / "bronze"))
    monkeypatch.setenv("HONEYPOT_DATA_DIR", str(data))
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    return tmp_path
