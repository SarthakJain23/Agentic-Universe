"""SQLite connection, schema bootstrap and a transaction helper.

WAL + synchronous=NORMAL: a killed *process* never loses a committed transaction (the crash model the
SRE memo cares about). Power loss could drop the last transaction; on restart we re-ingest it from the
simulator, so that is safe too.
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from pathlib import Path

_SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: str | Path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-200000")  # ~200 MB page cache
    conn.executescript(_SCHEMA.read_text())
    return conn


@contextlib.contextmanager
def tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK on any exception. Not re-entrant on purpose."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))


def open_for_run(path: str | Path, fingerprint: str) -> sqlite3.Connection:
    """Open the store for this simulator run. A store left over from a *different* run (other scenario,
    or a restarted simulator = new world) is archived, never mixed in."""
    path = Path(path)
    if path.exists():
        conn = connect(path)
        fp = get_meta(conn, "fingerprint")
        if fp in (None, fingerprint):
            if fp is None:
                set_meta(conn, "fingerprint", fingerprint)
            return conn
        conn.close()
        archived = path.with_name(f"{path.stem}.{int(time.time())}{path.suffix}")
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(path) + suffix)
            if p.exists():
                p.rename(Path(str(archived) + suffix))
    conn = connect(path)
    set_meta(conn, "fingerprint", fingerprint)
    return conn
