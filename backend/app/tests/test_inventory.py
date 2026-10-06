"""Persistence tests: every write-back assertion reads qty_remain/status back
out of a real database through a fresh connection — nothing is trusted from
return values."""
import threading

import pytest
from fastapi.testclient import TestClient

from app import inventory, seed
from app.db import connect
from app.engines.fefo import consume_fefo
from app.main import app


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    seed.init_db()
    c = connect()
    c.execute("DELETE FROM lots")
    c.execute("DELETE FROM consumptions")
    c.close()
    return tmp_path


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


def add_lot(item_id, qty, expiry):
    c = connect()
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
        " VALUES (?,?,?,?,?,?)",
        (item_id, qty, qty, expiry, "on_shelf", "clean"))
    lot_id = cur.lastrowid
    c.close()
    return lot_id


def read_lots():
    c = connect()
    rows = {r["id"]: dict(r) for r in c.execute("SELECT * FROM lots")}
    c.close()
    return rows


def read_consumptions():
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM consumptions")]
    c.close()
    return rows


# --- write-back: assertions read the remainder back from the DB ---

def test_apply_deductions_readback(db):
    early = add_lot(1, 2, "2026-01-10")
    late = add_lot(1, 3, "2026-02-01")
    c = connect()
    with inventory.atomic(c):
        lots = inventory.load_on_shelf_lots(c, 1)
        plan = consume_fefo(lots, 4)
        assert plan["ok"]
        inventory.apply_deductions(c, plan["deductions"])
    c.close()
    lots = read_lots()
    assert lots[early]["qty_remain"] == 0
    assert lots[early]["status"] == "consumed"
    assert lots[late]["qty_remain"] == 1
    assert lots[late]["status"] == "on_shelf"


def test_mark_expired_readback(db):
    expired = add_lot(1, 1, "2020-01-01")
    keep = add_lot(1, 1, "2030-01-01")
    c = connect()
    with inventory.atomic(c):
        inventory.mark_expired(c, [expired])
    c.close()
    lots = read_lots()
    assert lots[expired]["status"] == "expired"
    assert lots[expired]["qty_remain"] == 1
    assert lots[keep]["status"] == "on_shelf"


# --- route regression: pre-refactor main path stays locked ---

def test_consume_deducts_earliest_expiry_first(client):
    early = add_lot(1, 2, "2026-01-10")
    late = add_lot(1, 3, "2026-02-01")
    r = client.post("/api/consume", json={"item_id": 1, "qty": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["deductions"][0]["lot_id"] == early
    assert body["deductions"][0]["take"] == 2
    lots = read_lots()
    assert lots[early]["qty_remain"] == 0 and lots[early]["status"] == "consumed"
    assert lots[late]["qty_remain"] == 2 and lots[late]["status"] == "on_shelf"
    assert len(read_consumptions()) == 1


def test_consume_non_positive_fails(client):
    lid = add_lot(1, 2, "2026-01-10")
    for bad in (0, -1):
        r = client.post("/api/consume", json={"item_id": 1, "qty": bad})
        assert r.status_code == 400
    assert read_lots()[lid]["qty_remain"] == 2
    assert read_consumptions() == []


def test_expire_sweep_readback(client):
    expired = add_lot(1, 1, "2020-01-01")
    fresh = add_lot(1, 1, "2030-01-01")
    r = client.post("/api/expire-sweep")
    assert r.status_code == 200
    assert r.json()["expired_ids"] == [expired]
    lots = read_lots()
    assert lots[expired]["status"] == "expired"
    assert lots[fresh]["status"] == "on_shelf"


# --- atomicity: no half-written units on mid-persistence failure ---

def test_failure_after_lot_updates_rolls_back_everything(db, monkeypatch):
    early = add_lot(1, 2, "2026-01-10")
    late = add_lot(1, 3, "2026-02-01")

    def boom(conn, note, result):
        raise RuntimeError("db died after lot updates")
    monkeypatch.setattr(inventory, "record_consumption", boom)

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/api/consume", json={"item_id": 1, "qty": 4})
    assert r.status_code == 500
    lots = read_lots()
    assert lots[early]["qty_remain"] == 2 and lots[early]["status"] == "on_shelf"
    assert lots[late]["qty_remain"] == 3 and lots[late]["status"] == "on_shelf"
    assert read_consumptions() == []


def test_failure_mid_deduction_plan_leaves_no_partial_write(db, monkeypatch):
    early = add_lot(1, 2, "2026-01-10")
    late = add_lot(1, 3, "2026-02-01")
    real = inventory.apply_deductions

    def boom(conn, deductions):
        real(conn, deductions[:1])  # first lot update really lands...
        raise RuntimeError("db died mid-plan")  # ...then persistence dies
    monkeypatch.setattr(inventory, "apply_deductions", boom)

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/api/consume", json={"item_id": 1, "qty": 4})
    assert r.status_code == 500
    lots = read_lots()
    assert lots[early]["qty_remain"] == 2 and lots[early]["status"] == "on_shelf"
    assert lots[late]["qty_remain"] == 3 and lots[late]["status"] == "on_shelf"
    assert read_consumptions() == []


def test_failure_mid_sweep_leaves_no_partial_expired(db, monkeypatch):
    a = add_lot(1, 1, "2020-01-01")
    b = add_lot(1, 1, "2020-06-01")

    def boom(conn, ids):
        conn.execute("UPDATE lots SET status='expired' WHERE id=?", (ids[0],))
        raise RuntimeError("db died mid-sweep")
    monkeypatch.setattr(inventory, "mark_expired", boom)

    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/api/expire-sweep")
    assert r.status_code == 500
    lots = read_lots()
    assert lots[a]["status"] == "on_shelf"
    assert lots[b]["status"] == "on_shelf"


# --- short stock: all-or-nothing, consistent with a concurrent second consume ---

def test_short_consume_writes_nothing(client):
    lid = add_lot(1, 1, "2026-01-10")
    r = client.post("/api/consume", json={"item_id": 1, "qty": 5})
    assert r.status_code == 409
    assert r.json()["detail"]["short"] == 4
    # all-or-nothing: the calculated partial deductions are not persisted
    assert read_lots()[lid]["qty_remain"] == 1
    assert read_consumptions() == []


def test_concurrent_consume_reads_committed_remainders(client):
    add_lot(1, 3, "2026-01-10")
    results = []

    def hit():
        results.append(client.post("/api/consume", json={"item_id": 1, "qty": 2}).status_code)

    t1 = threading.Thread(target=hit)
    t2 = threading.Thread(target=hit)
    t1.start(); t2.start(); t1.join(); t2.join()

    # the second consume plans against what the first committed: exactly one fits
    assert sorted(results) == [200, 409]
    assert sum(l["qty_remain"] for l in read_lots().values()) == 1
    assert len(read_consumptions()) == 1
