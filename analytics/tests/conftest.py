"""Shared fixtures.

Each test gets its own HONEYPOT_DATA_DIR so nothing leaks between them, and
db.connect() keeps its extensions under that directory. Left alone that means
every test downloads httpfs and postgres again, which costs more than the tests
themselves. The cache below fetches them once per session and hands each test a
copy.
"""

from __future__ import annotations

import shutil

import pytest

import duckdb


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
