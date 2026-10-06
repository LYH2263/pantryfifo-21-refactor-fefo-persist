"""Routes only orchestrate: status mapping on top of the shelf module.

Calls the route callables directly (no HTTP client dependency needed);
verifies 400 on invalid qty, 409 on shortage, and delegation to the
persistence module on success.
"""

import inspect

import pytest
from fastapi import HTTPException

from app.db import connect
from app.main import ConsumeIn, consume, expire_sweep


def add_lot(item_id, qty, expiry):
    c = connect()
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
        " VALUES (?,?,?,?,?,?)",
        (item_id, qty, qty, expiry, "on_shelf", "clean"))
    c.commit()
    lid = cur.lastrowid
    c.close()
    return lid


def test_consume_route_success_delegates_to_shelf(db):
    early = add_lot(1, 2, "2026-10-10")
    later = add_lot(1, 3, "2026-11-01")

    out = consume(ConsumeIn(item_id=1, qty=3))

    assert out["ok"]
    c = connect()
    early_row = c.execute("SELECT qty_remain,status FROM lots WHERE id=?", (early,)).fetchone()
    later_row = c.execute("SELECT qty_remain FROM lots WHERE id=?", (later,)).fetchone()
    assert dict(early_row)["status"] == "consumed"
    assert dict(later_row)["qty_remain"] == 2
    assert c.execute("SELECT COUNT(*) n FROM consumptions").fetchone()["n"] == 1
    c.close()


def test_consume_route_maps_non_positive_to_400(db):
    add_lot(1, 2, "2026-10-10")
    with pytest.raises(HTTPException) as ei:
        consume(ConsumeIn(item_id=1, qty=0))
    assert ei.value.status_code == 400


def test_consume_route_maps_short_to_409(db):
    add_lot(1, 1, "2026-10-10")
    with pytest.raises(HTTPException) as ei:
        consume(ConsumeIn(item_id=1, qty=5))
    assert ei.value.status_code == 409
    assert ei.value.detail["short"] == 4


def test_consume_route_has_no_sql_of_its_own():
    from app import main
    src = inspect.getsource(main.consume)
    assert "execute(" not in src and "connect(" not in src


def test_expire_sweep_route_delegates(db):
    due = add_lot(1, 1, "2026-09-01")
    out = expire_sweep()
    assert due in out["expired_ids"]
    c = connect()
    assert c.execute("SELECT status FROM lots WHERE id=?", (due,)).fetchone()["status"] == "expired"
    c.close()
