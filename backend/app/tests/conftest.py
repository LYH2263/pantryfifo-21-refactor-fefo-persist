"""Shared fixtures for persistence/route tests.

A real on-disk SQLite file (cross-thread visibility matters for the
concurrency test), freshly seeded per test. ``synchronous=OFF`` only drops
fsync-per-commit on this slow CI filesystem; transaction/ROLLBACK
semantics — what the atomicity tests assert — are unchanged.
"""

import pytest

from app import db as db_mod
from app import seed
from app.modules import shelf

TODAY = "2026-10-05"


@pytest.fixture()
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    real_connect = db_mod.connect

    def fast_connect():
        c = real_connect()
        c.execute("PRAGMA synchronous=OFF")
        return c

    monkeypatch.setattr(db_mod, "connect", fast_connect)
    # seed.py binds connect at import time ("from app.db import connect").
    monkeypatch.setattr("app.seed.connect", fast_connect)
    monkeypatch.setattr(shelf, "connect", fast_connect)
    monkeypatch.setattr(shelf, "_today", lambda: TODAY)

    seed.init_db()
    c = fast_connect()
    c.execute("DELETE FROM lots")
    c.execute("DELETE FROM consumptions")
    c.commit()
    c.close()
    yield tmp_path
