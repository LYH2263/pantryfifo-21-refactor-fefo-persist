"""FEFO consume: earliest expiry first among positive remaining lots.

Backward-compatible facade. The calculus now lives, database-free, in
:mod:`app.engines.expiry`; consume and off-shelf flows share the same
``expiry_order`` there. These wrappers keep the original signatures and
result shapes for existing callers and tests.
"""

from app.engines.expiry import expiry_order, partition_expired, take

__all__ = ["sort_lots_fefo", "consume_fefo", "expire_lots"]


def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    return expiry_order(lots)


def consume_fefo(lots: list[dict], qty: float) -> dict:
    """Return deductions list and leftover demand. Mutates copies only."""
    qty = float(qty)
    if qty <= 0:
        return {"ok": False, "reason": "qty_non_positive",
                "deductions": [], "short": 0.0}
    allocation = take(expiry_order(lots), qty)
    return {
        "ok": allocation["ok"],
        "reason": allocation["reason"],
        "deductions": allocation["deductions"],
        "short": allocation["short"],
    }


def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf: remaining>0 and expiry < today."""
    expired, _ = partition_expired(lots, today)
    return [l["id"] for l in expired]
