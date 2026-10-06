"""Zero-DB tests for the pure expiry/FEFO calculus.

The module under test must not open a database: it must not import sqlite3
nor app.db. These tests run entirely on plain dicts.
"""

import inspect

import app.engines.expiry as expiry
from app.engines.expiry import expiry_order, partition_expired, plan_consume, take
from app.engines.fefo import consume_fefo, expire_lots, sort_lots_fefo


def test_module_is_database_free():
    src = inspect.getsource(expiry)
    assert "sqlite3" not in src
    assert not hasattr(expiry, "connect")
    import app.engines.expiry as mod
    assert not any("app.db" in name for name in vars(mod))


def test_expiry_order_earliest_first_and_tie_by_id():
    lots = [
        {"id": 3, "qty_remain": 1, "expiry": "2026-03-01"},
        {"id": 1, "qty_remain": 2, "expiry": "2026-01-10"},
        {"id": 2, "qty_remain": 2, "expiry": "2026-01-10"},
        {"id": 4, "qty_remain": 5, "expiry": None},
    ]
    assert [l["id"] for l in expiry_order(lots)] == [1, 2, 3, 4]


def test_non_positive_and_zero_remaining_lots_excluded():
    assert expiry_order([
        {"id": 1, "qty_remain": 0, "expiry": "2026-01-01"},
        {"id": 2, "qty_remain": -1, "expiry": "2026-01-02"},
    ]) == []


def test_take_spends_earliest_lot_first():
    lots = [
        {"id": 2, "qty_remain": 3, "expiry": "2026-02-01"},
        {"id": 1, "qty_remain": 2, "expiry": "2026-01-10"},
    ]
    r = take(expiry_order(lots), 3)
    assert r["ok"]
    assert [d["lot_id"] for d in r["deductions"]] == [1, 2]
    assert r["deductions"][0]["take"] == 2 and r["deductions"][0]["exhaust"] is True
    assert r["deductions"][1]["take"] == 1 and r["deductions"][1]["exhaust"] is False


def test_take_does_not_mutate_inputs():
    lots = [{"id": 1, "qty_remain": 2, "expiry": "2026-01-10"}]
    take(expiry_order(lots), 1)
    assert lots[0]["qty_remain"] == 2


def test_take_short_reports_leftover():
    r = take([{"id": 1, "qty_remain": 1, "expiry": "2026-01-01"}], 5)
    assert r["ok"] is False and r["reason"] == "short" and r["short"] == 4


def test_partition_expired_boundary_is_strictly_before_today():
    lots = [
        {"id": 1, "qty_remain": 1, "expiry": "2026-09-30"},
        {"id": 2, "qty_remain": 1, "expiry": "2026-10-01"},
        {"id": 3, "qty_remain": 1, "expiry": "2026-10-02"},
    ]
    expired, fresh = partition_expired(lots, "2026-10-01")
    assert [l["id"] for l in expired] == [1]
    assert [l["id"] for l in fresh] == [2, 3]


def test_plan_consume_expires_first_then_consumes_fresh_in_same_order():
    lots = [
        {"id": 10, "qty_remain": 1, "expiry": "2026-09-01"},   # expired
        {"id": 11, "qty_remain": 2, "expiry": "2026-10-05"},   # shelfable, earliest
        {"id": 12, "qty_remain": 2, "expiry": "2026-11-01"},
    ]
    p = plan_consume(lots, 3, "2026-10-05")
    assert p["ok"]
    assert p["expired_ids"] == [10]
    assert [d["lot_id"] for d in p["deductions"]] == [11, 12]
    assert [d["take"] for d in p["deductions"]] == [2, 1]
    assert all(d["exhaust"] for d in p["deductions"][:1])


def test_plan_consume_does_not_count_expired_stock_toward_demand():
    p = plan_consume(
        [{"id": 1, "qty_remain": 5, "expiry": "2026-09-01"}], 4, "2026-10-05")
    assert p["ok"] is False and p["reason"] == "short" and p["short"] == 4
    assert p["expired_ids"] == [1] and p["deductions"] == []


def test_plan_consume_non_positive_fails_without_expired_plan():
    p = plan_consume([{"id": 1, "qty_remain": 1, "expiry": "2026-01-01"}], 0, "2026-10-05")
    assert p["ok"] is False and p["reason"] == "qty_non_positive"
    assert p["deductions"] == [] and p["expired_ids"] == []


# --- Regression: the pre-refactor main path, via the old facade ----------

def test_regression_fefo_order():
    lots = [
        {"id": 2, "qty_remain": 3, "expiry": "2026-02-01"},
        {"id": 1, "qty_remain": 2, "expiry": "2026-01-10"},
    ]
    assert [l["id"] for l in sort_lots_fefo(lots)] == [1, 2]
    r = consume_fefo(lots, 3)
    assert r["ok"] and r["deductions"][0]["lot_id"] == 1
    assert r["deductions"][0]["take"] == 2 and r["deductions"][1]["take"] == 1


def test_regression_short_and_non_positive():
    r = consume_fefo([{"id": 1, "qty_remain": 1, "expiry": "2026-01-01"}], 5)
    assert r["ok"] is False and r["short"] == 4
    bad = consume_fefo([{"id": 1, "qty_remain": 1, "expiry": "2026-01-01"}], 0)
    assert bad["ok"] is False and bad["reason"] == "qty_non_positive"


def test_regression_expire_lots():
    assert expire_lots([
        {"id": 1, "qty_remain": 1, "expiry": "2025-01-01"},
        {"id": 2, "qty_remain": 1, "expiry": "2027-01-01"},
    ], "2026-01-01") == [1]
