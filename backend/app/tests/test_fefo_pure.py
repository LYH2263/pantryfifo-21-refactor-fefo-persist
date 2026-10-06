"""Pure planning tests: no database is opened anywhere in this file."""
from app.engines.fefo import consume_fefo, expire_lots, sort_lots_fefo

def test_consume_follows_shared_expiry_order():
    lots = [
        {"id": 3, "qty_remain": 5, "expiry": "2026-03-01"},
        {"id": 1, "qty_remain": 5, "expiry": "2026-01-01"},
        {"id": 2, "qty_remain": 5, "expiry": "2026-02-01"},
    ]
    plan = consume_fefo(lots, 7)
    assert plan["ok"]
    assert [d["lot_id"] for d in plan["deductions"]] == [1, 2]
    assert [d["take"] for d in plan["deductions"]] == [5, 2]

def test_expire_sweep_uses_same_order_as_consume():
    lots = [
        {"id": 9, "qty_remain": 1, "expiry": "2026-01-05"},
        {"id": 4, "qty_remain": 1, "expiry": "2026-01-01"},
        {"id": 7, "qty_remain": 1, "expiry": "2026-01-03"},
    ]
    assert expire_lots(lots, "2026-02-01") == [4, 7, 9]
    assert [l["id"] for l in sort_lots_fefo(lots)] == [4, 7, 9]

def test_tie_break_by_id_shared():
    lots = [
        {"id": 2, "qty_remain": 1, "expiry": "2026-01-01"},
        {"id": 1, "qty_remain": 1, "expiry": "2026-01-01"},
    ]
    assert expire_lots(lots, "2026-02-01") == [1, 2]
    plan = consume_fefo(lots, 1)
    assert plan["deductions"][0]["lot_id"] == 1

def test_expire_skips_empty_and_unexpired():
    lots = [
        {"id": 1, "qty_remain": 0, "expiry": "2020-01-01"},
        {"id": 2, "qty_remain": 1, "expiry": None},
        {"id": 3, "qty_remain": 1, "expiry": "2030-01-01"},
        {"id": 4, "qty_remain": 1, "expiry": "2020-06-01"},
    ]
    assert expire_lots(lots, "2026-01-01") == [4]
