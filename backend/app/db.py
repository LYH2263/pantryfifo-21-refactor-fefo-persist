import os, sqlite3
from pathlib import Path

def db_path() -> Path:
    d = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "pantryfifo.db"

def connect():
    # Autocommit mode: multi-statement writes are wrapped explicitly in
    # inventory.atomic(); busy_timeout lets a concurrent consumer wait for the
    # in-flight transaction instead of failing on a locked database.
    c = sqlite3.connect(db_path(), isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=5000")
    return c
