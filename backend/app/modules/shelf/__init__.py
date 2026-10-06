"""Persistence for expiry-first consume and off-shelf sweeps.

The pure calculus lives in :mod:`app.engines.expiry`; everything here is
write-back against SQLite. Each operation runs in ONE transaction:

* the same expiry ordering drives both the ``expired`` status flips and the
  consume deductions, so consume and off-shelf can never disagree;
* ``BEGIN IMMEDIATE`` takes the write lock before re-reading, therefore a
  second concurrent consume sees the first one's committed remaining — the
  plan and the write always use the same numbers;
* either every change (lot statuses / qty_remain / consumptions row) lands
  or, on any failure, the transaction rolls back and no half-written state
  remains.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

from app.db import connect
from app.engines.expiry import partition_expired, plan_consume


class ShelfError(Exception):
    """Invalid request (e.g. non-positive qty). Maps to HTTP 400."""


class ShelfShort(Exception):
    """Not enough shelfable quantity. Nothing was written. Maps to HTTP 409."""

    def __init__(self, plan: dict):
        super().__init__("short")
        self.plan = plan


_BUSY_TIMEOUT_MS = 10_000


def _tx_connect():
    """Connection for a write transaction.

    ``busy_timeout`` makes a concurrent second consume WAIT for the first
    transaction instead of failing with SQLITE_BUSY; once it gets the write
    lock it re-reads the committed remaining, so its plan matches reality.
    """
    c = connect()
    c.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return c


def _today() -> str:
    return date.today().isoformat()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_lots(conn, item_id: int | None = None) -> list[dict]:
    if item_id is None:
        rows = conn.execute("SELECT * FROM lots WHERE status='on_shelf'")
    else:
        rows = conn.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf'",
            (item_id,))
    return [dict(r) for r in rows]


def _write_expired(conn, expired_ids: list[int]) -> None:
    conn.executemany(
        "UPDATE lots SET status='expired' WHERE id=? AND status='on_shelf'",
        [(i,) for i in expired_ids])


def _write_deductions(conn, deductions: list[dict]) -> None:
    for d in deductions:
        # Guard: never deduct more than the remaining quantity observed
        # under the write lock. If a guard ever fired, the calculus ran on
        # stale data — raise so the whole transaction rolls back.
        cur = conn.execute(
            """UPDATE lots
               SET qty_remain = CASE WHEN qty_remain - ? <= 0 THEN 0
                                     ELSE qty_remain - ? END,
                   status = CASE WHEN qty_remain - ? <= 0 THEN 'consumed'
                                 ELSE status END
               WHERE id=? AND status='on_shelf' AND qty_remain >= ?""",
            (d["take"], d["take"], d["take"], d["lot_id"], d["take"]))
        if cur.rowcount != 1:
            raise RuntimeError(f"lot {d['lot_id']} changed under lock")


def _read_back(conn, lot_ids: list[int]) -> list[dict]:
    if not lot_ids:
        return []
    marks = ",".join("?" for _ in lot_ids)
    return [dict(r) for r in conn.execute(
        f"SELECT id, qty_remain, status FROM lots WHERE id IN ({marks})",
        lot_ids)]


def apply_consume(item_id: int, qty, note: str = "", *, today: str | None = None) -> dict:
    """Atomically expire due lots, FEFO-consume qty, record the order.

    On shortage nothing is written and :class:`ShelfShort` is raised.
    Returns the plan plus read-back ``lots`` (qty_remain/status from the
    committed transaction).
    """
    today = today or _today()
    c = _tx_connect()
    try:
        c.isolation_level = None
        c.execute("BEGIN IMMEDIATE")
        lots = _read_lots(c, item_id)
        plan = plan_consume(lots, qty, today)
        if not plan["ok"] and plan["reason"] == "qty_non_positive":
            raise ShelfError("qty_non_positive")
        if not plan["ok"]:
            raise ShelfShort(plan)

        _write_expired(c, plan["expired_ids"])
        _write_deductions(c, plan["deductions"])
        c.execute(
            "INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
            (note, json.dumps(plan), _now()))
        affected = sorted(set(plan["expired_ids"]) |
                          {d["lot_id"] for d in plan["deductions"]})
        readback = _read_back(c, affected)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    return {**plan, "lots": readback}


def sweep_expired(*, today: str | None = None) -> dict:
    """Atomically flip every on-shelf lot whose expiry date has passed."""
    today = today or _today()
    c = _tx_connect()
    try:
        c.isolation_level = None
        c.execute("BEGIN IMMEDIATE")
        expired, _ = partition_expired(_read_lots(c), today)
        expired_ids = [l["id"] for l in expired]
        _write_expired(c, expired_ids)
        readback = _read_back(c, expired_ids)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    return {"expired_ids": expired_ids, "lots": readback}
