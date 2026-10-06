"""Expiry-first (FEFO) shelf calculus.

Pure module: it never opens a database and never mutates its inputs — lots
are plain dicts in, plain data structures out. Both the consume flow and
the expire/off-shelf flow derive from ``expiry_order`` so they share one
definition of "earliest due first".

Persistence of the plans produced here lives in :mod:`app.modules.shelf`.
"""

from __future__ import annotations

EPS = 1e-9
# Missing expiry sorts after every real date.
_NO_EXPIRY = "9999-12-31"


def _has_positive_remain(lot: dict) -> bool:
    try:
        return float(lot.get("qty_remain", 0)) > 0
    except (TypeError, ValueError):
        return False


def expiry_order(lots: list[dict]) -> list[dict]:
    """Positive-remainder lots, earliest expiry first (NULL expiry last).

    Ties break by lot id so the order is total and deterministic.
    """
    return sorted(
        (l for l in lots if _has_positive_remain(l)),
        key=lambda l: (l.get("expiry") or _NO_EXPIRY, l.get("id") or 0),
    )


def partition_expired(lots: list[dict], today: str) -> tuple[list[dict], list[dict]]:
    """Split positive lots into ``(expired, shelfable)`` in expiry order.

    A lot whose expiry equals ``today`` is still shelfable — only strictly
    earlier dates leave the shelf.
    """
    expired, fresh = [], []
    for lot in expiry_order(lots):
        exp = lot.get("expiry")
        if exp is not None and exp < today:
            expired.append(lot)
        else:
            fresh.append(lot)
    return expired, fresh


def take(ordered_lots: list[dict], qty: float) -> dict:
    """Allocate ``qty`` across already-ordered lots. Inputs are not mutated."""
    need = float(qty)
    deductions: list[dict] = []
    for lot in ordered_lots:
        if need <= EPS:
            break
        avail = float(lot["qty_remain"])
        if avail <= 0:
            continue
        amount = min(avail, need)
        deductions.append({
            "lot_id": lot["id"],
            "take": amount,
            "expiry": lot.get("expiry"),
            "exhaust": amount >= avail - EPS,
        })
        need -= amount
    if need > EPS:
        return {"ok": False, "reason": "short", "deductions": deductions,
                "short": round(need, 3)}
    return {"ok": True, "reason": "", "deductions": deductions, "short": 0.0}


def _failed(reason: str) -> dict:
    return {"ok": False, "reason": reason, "short": 0.0, "expired": [],
            "expired_ids": [], "deductions": []}


def plan_consume(lots: list[dict], qty, today: str) -> dict:
    """Plan one consume order from one shared expiry ordering.

    Expired lots are taken off the shelf first (same expiry order); only
    still-shelfable lots are offered to the FEFO ``take`` calculus. A short
    plan still carries the attempted ``deductions`` and the ``short`` amount
    so callers can report them, but the persistence layer writes nothing for
    it — the plan itself is not a write.
    """
    try:
        qty = float(qty)
    except (TypeError, ValueError):
        return _failed("qty_non_positive")
    if qty <= 0:
        return _failed("qty_non_positive")
    expired, fresh = partition_expired(lots, today)
    allocation = take(fresh, qty)
    return {
        "ok": allocation["ok"],
        "reason": allocation["reason"],
        "short": allocation["short"],
        "expired": [
            {"lot_id": l["id"], "expiry": l.get("expiry"),
             "qty_remain": float(l["qty_remain"])}
            for l in expired
        ],
        "expired_ids": [l["id"] for l in expired],
        "deductions": allocation["deductions"],
    }
