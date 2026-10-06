"""FEFO planning: one shared expiry ordering plus pure take/expire calculation.

This module never opens a database. Routes load lot rows, hand them here as
plain dicts, and persist the returned plan via app.inventory.
"""

def sort_lots_fefo(lots: list[dict]) -> list[dict]:
    """The single expiry ordering shared by consume and expire-sweep:
    only lots with positive remaining qty, earliest expiry first, ties by id."""
    return sorted(
        [l for l in lots if float(l.get("qty_remain", 0)) > 0],
        key=lambda l: (l.get("expiry") or "9999-99-99", l.get("id") or 0),
    )

def consume_fefo(lots: list[dict], qty: float) -> dict:
    """Take calculation only: deductions list and leftover demand. Writes nothing."""
    need = float(qty)
    if need <= 0:
        return {"ok": False, "reason": "qty_non_positive", "deductions": [], "short": 0.0}
    ordered = sort_lots_fefo(lots)
    deductions = []
    for lot in ordered:
        if need <= 0:
            break
        avail = float(lot["qty_remain"])
        take = min(avail, need)
        deductions.append({"lot_id": lot["id"], "take": take, "expiry": lot.get("expiry")})
        need -= take
    if need > 1e-9:
        return {"ok": False, "reason": "short", "deductions": deductions, "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}

def expire_lots(lots: list[dict], today: str) -> list[int]:
    """Ids that should leave shelf (remaining>0 and expiry < today),
    returned in the same FEFO order consume_fefo deducts by."""
    expired = [l for l in lots if l.get("expiry") and l["expiry"] < today]
    return [l["id"] for l in sort_lots_fefo(expired)]
