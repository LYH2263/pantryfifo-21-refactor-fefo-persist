"""Persistence tests for app.modules.shelf.

Every write test READS BACK qty_remain/status through a fresh database
connection; it never trusts the call's return value alone. Covers:

* FEFO main path: earliest lot first, exhausted lot -> consumed;
* non-positive / short: the whole order is not written;
* mid-write failure: expired flips, deductions and the consumptions row
  appear all together or not at all;
* two concurrent consumes: the loser reads the winner's committed remaining
  and writes nothing.
"""

import threading

import pytest

from app.db import connect
from app.modules import shelf
from app.modules.shelf import ShelfError, ShelfShort

TODAY = "2026-10-05"


def add_lot(item_id, qty, expiry, status="on_shelf"):
    c = connect()
    cur = c.execute(
        "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
        " VALUES (?,?,?,?,?,?)",
        (item_id, qty, qty, expiry, status, "clean"))
    c.commit()
    lid = cur.lastrowid
    c.close()
    return lid


def get_lot(lot_id):
    c = connect()
    row = dict(c.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone())
    c.close()
    return row


def consumption_count():
    c = connect()
    n = c.execute("SELECT COUNT(*) n FROM consumptions").fetchone()["n"]
    c.close()
    return n


# --- main path: read back remaining and status ---------------------------

def test_consume_deducts_earliest_first_and_exhausted_lot_is_consumed(db):
    early = add_lot(1, 2, "2026-10-10")
    later = add_lot(1, 3, "2026-11-01")

    out = shelf.apply_consume(1, 3, today=TODAY)

    assert [d["lot_id"] for d in out["deductions"]] == [early, later]
    # Read back from the database, not from the return value.
    assert get_lot(early)["qty_remain"] == 0
    assert get_lot(early)["status"] == "consumed"
    assert get_lot(later)["qty_remain"] == 2
    assert get_lot(later)["status"] == "on_shelf"
    assert consumption_count() == 1
    # Returned read-back agrees with the actual committed rows.
    assert {x["id"]: (x["qty_remain"], x["status"]) for x in out["lots"]} == {
        early: (0, "consumed"), later: (2, "on_shelf")}


def test_consume_expires_due_lots_in_same_txn_and_skips_them(db):
    due = add_lot(1, 4, "2026-09-01")
    fresh = add_lot(1, 2, "2026-10-10")

    out = shelf.apply_consume(1, 2, today=TODAY)

    assert out["expired_ids"] == [due]
    assert [d["lot_id"] for d in out["deductions"]] == [fresh]
    assert get_lot(due)["status"] == "expired"
    assert get_lot(due)["qty_remain"] == 4          # expired stock is not consumed
    assert get_lot(fresh)["qty_remain"] == 0
    assert get_lot(fresh)["status"] == "consumed"
    assert consumption_count() == 1


def test_non_positive_qty_writes_nothing(db):
    lot = add_lot(1, 2, "2026-10-10")
    due = add_lot(1, 1, "2026-09-01")
    with pytest.raises(ShelfError):
        shelf.apply_consume(1, 0, today=TODAY)
    assert get_lot(lot)["qty_remain"] == 2 and get_lot(lot)["status"] == "on_shelf"
    assert get_lot(due)["status"] == "on_shelf"      # not even expired flips
    assert consumption_count() == 0


# --- shortage: whole order is not written ---------------------------------

def test_short_order_writes_nothing_even_though_partial_was_calculable(db):
    early = add_lot(1, 1, "2026-10-10")
    later = add_lot(1, 1, "2026-11-01")
    due = add_lot(1, 5, "2026-09-01")

    with pytest.raises(ShelfShort) as ei:
        shelf.apply_consume(1, 3, today=TODAY)
    # The plan reports what WOULD have been taken from shelfable lots...
    assert ei.value.plan["short"] == 1
    assert [d["lot_id"] for d in ei.value.plan["deductions"]] == [early, later]

    # ...but none of it is written, and the expired flip is rolled back too.
    assert get_lot(early)["qty_remain"] == 1 and get_lot(early)["status"] == "on_shelf"
    assert get_lot(later)["qty_remain"] == 1 and get_lot(later)["status"] == "on_shelf"
    assert get_lot(due)["qty_remain"] == 5 and get_lot(due)["status"] == "on_shelf"
    assert consumption_count() == 0


# --- atomicity under mid-write failure ------------------------------------

import sqlite3

from app.db import db_path


class FailingConn(sqlite3.Connection):
    """Real connection subclass that fails SQL matching a rule once armed."""

    fail_substr: str | None = None
    fail_nth_update: int | None = None
    _updates_seen = 0

    def execute(self, sql, params=()):
        if self.fail_substr and self.fail_substr in sql:
            raise RuntimeError("injected failure")
        if self.fail_nth_update is not None and "UPDATE lots" in sql:
            self._updates_seen += 1
            if self._updates_seen == self.fail_nth_update:
                raise RuntimeError("injected failure")
        return super().execute(sql, params)


def _failing_connect(**arm):
    c = sqlite3.connect(db_path(), factory=FailingConn)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA synchronous=OFF")
    c.fail_substr = arm.get("fail_substr")
    c.fail_nth_update = arm.get("fail_nth_update")
    return c


def test_failure_before_commit_rolls_back_expired_deductions_and_consumption_row(
        db, monkeypatch):
    due = add_lot(1, 1, "2026-09-01")
    fresh = add_lot(1, 2, "2026-10-10")
    monkeypatch.setattr(
        shelf, "connect", lambda: _failing_connect(fail_substr="INSERT INTO consumptions"))

    with pytest.raises(RuntimeError):
        shelf.apply_consume(1, 2, today=TODAY)

    # No half-written state: no deduction, no expired flip, no consumption row.
    assert get_lot(fresh)["qty_remain"] == 2 and get_lot(fresh)["status"] == "on_shelf"
    assert get_lot(due)["status"] == "on_shelf" and get_lot(due)["qty_remain"] == 1
    assert consumption_count() == 0


def test_failure_midway_through_deductions_rolls_back_everything(db, monkeypatch):
    due = add_lot(1, 1, "2026-09-01")
    first = add_lot(1, 1, "2026-10-10")
    second = add_lot(1, 1, "2026-11-01")
    # The expired flip goes through executemany; fail on the 2nd deduction
    # execute, after the first deduction has already been issued.
    monkeypatch.setattr(shelf, "connect", lambda: _failing_connect(fail_nth_update=2))

    with pytest.raises(RuntimeError):
        shelf.apply_consume(1, 2, today=TODAY)

    assert get_lot(first)["qty_remain"] == 1 and get_lot(first)["status"] == "on_shelf"
    assert get_lot(second)["qty_remain"] == 1 and get_lot(second)["status"] == "on_shelf"
    assert get_lot(due)["status"] == "on_shelf"
    assert consumption_count() == 0


# --- concurrency: loser reads winner's committed remaining ----------------

def test_concurrent_consumes_leave_no_half_state(db):
    add_lot(1, 2, "2026-10-10")
    add_lot(1, 1, "2026-11-01")

    outcomes: list[dict] = []
    outcomes_lock = threading.Lock()

    def worker():
        try:
            shelf.apply_consume(1, 2, today=TODAY)
            with outcomes_lock:
                outcomes.append({"ok": True})
        except ShelfShort as e:
            with outcomes_lock:
                outcomes.append({"ok": False, "short": e.plan["short"]})

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outcomes) == 2
    winners = [o for o in outcomes if o["ok"]]
    losers = [o for o in outcomes if not o["ok"]]
    assert len(winners) == 1
    # The loser waited for the lock, re-read committed remaining (1 vs demand
    # 2), and failed as a clean shortage — not a lock/serialization error.
    assert losers == [{"ok": False, "short": 1}]
    c = connect()
    total = c.execute("SELECT COALESCE(SUM(qty_remain),0) s FROM lots WHERE item_id=1").fetchone()["s"]
    statuses = [r["status"] for r in c.execute("SELECT status FROM lots WHERE item_id=1")]
    rows = c.execute("SELECT COUNT(*) n FROM consumptions").fetchone()["n"]
    c.close()
    # Winner consumed 2 out of 3; loser saw that committed remaining and wrote nothing.
    assert total == 1
    assert statuses.count("consumed") == 1 and statuses.count("on_shelf") == 1
    assert rows == 1


# --- off-shelf sweep reads back and is idempotent -------------------------

def test_sweep_flips_only_expired_and_reads_back(db):
    due1 = add_lot(1, 1, "2026-09-30")
    due2 = add_lot(2, 2, "2026-10-04")
    fresh = add_lot(1, 3, "2026-10-05")   # expiry == today stays on shelf

    out = shelf.sweep_expired(today=TODAY)

    assert sorted(out["expired_ids"]) == sorted([due1, due2])
    assert get_lot(due1)["status"] == "expired" and get_lot(due1)["qty_remain"] == 1
    assert get_lot(due2)["status"] == "expired" and get_lot(due2)["qty_remain"] == 2
    assert get_lot(fresh)["status"] == "on_shelf" and get_lot(fresh)["qty_remain"] == 3

    again = shelf.sweep_expired(today=TODAY)
    assert again["expired_ids"] == []
