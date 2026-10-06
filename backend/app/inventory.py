"""Persistence for the inventory: the only module that writes lots.qty_remain,
lots.status and the consumptions log.

Every write path runs inside atomic(), so a failure mid-persistence rolls the
whole unit back — readers never observe deductions without their consumptions
row, half of a multi-lot deduction plan, or a half-swept set of expired lots.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

@contextmanager
def atomic(conn):
    """One immediate-write transaction.

    BEGIN IMMEDIATE takes the write lock up front, so a concurrent second
    consumer waits here and then plans against the remainders this
    transaction committed — never against a half-written state.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    else:
        conn.execute("COMMIT")

def load_on_shelf_lots(conn, item_id: int | None = None) -> list[dict]:
    """Read lots for the pure planner. Must be called inside atomic()."""
    q = "SELECT * FROM lots WHERE status='on_shelf'"
    args: list = []
    if item_id is not None:
        q += " AND item_id=? AND qty_remain>0"
        args.append(item_id)
    return [dict(r) for r in conn.execute(q, args)]

def apply_deductions(conn, deductions: list[dict]) -> None:
    """Write a calculated deduction plan back: qty_remain down, empty lots consumed."""
    for d in deductions:
        conn.execute("UPDATE lots SET qty_remain = qty_remain - ? WHERE id=?", (d["take"], d["lot_id"]))
        rem = conn.execute("SELECT qty_remain FROM lots WHERE id=?", (d["lot_id"],)).fetchone()["qty_remain"]
        if rem <= 0:
            conn.execute("UPDATE lots SET status='consumed', qty_remain=0 WHERE id=?", (d["lot_id"],))

def record_consumption(conn, note: str, result: dict) -> None:
    conn.execute(
        "INSERT INTO consumptions(note,result_json,created_at) VALUES (?,?,?)",
        (note, json.dumps(result), datetime.now(timezone.utc).isoformat()))

def mark_expired(conn, lot_ids: list[int]) -> None:
    conn.executemany("UPDATE lots SET status='expired' WHERE id=?", [(i,) for i in lot_ids])
