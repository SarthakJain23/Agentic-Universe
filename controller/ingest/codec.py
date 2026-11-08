"""String <-> small-int dictionary and the lead -> features index used to join calls to lead features.

Both are caches over tables in the store; `reload()` rebuilds them after a restart or a rolled-back
transaction, so they can never disagree with what was committed.
"""
from __future__ import annotations

import sqlite3

import numpy as np

# Lead features, in the order they are packed into `feat` (from the SIMULATOR.md lead event contract).
FEATURES: tuple[str, ...] = ("lang", "city_tier", "age_band", "value_band")
UNKNOWN = 255


def unpack(feat: int) -> tuple[int, int, int, int]:
    return feat & 255, (feat >> 8) & 255, (feat >> 16) & 255, (feat >> 24) & 255


def id_number(s: str) -> int | None:
    """'nw_renewal:l00012345' -> 12345 ; anything unparseable -> None."""
    try:
        return int(s.rsplit(":", 1)[1][1:])
    except (IndexError, ValueError):
        return None


class Codec:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.reload()

    def reload(self) -> None:
        self._to_id: dict[tuple[str, str], int] = {}
        self._to_str: dict[tuple[str, int], str] = {}
        self._next: dict[str, int] = {}
        self._pending: list[tuple[str, str, int]] = []
        for kind, value, i in self.conn.execute("SELECT kind, value, id FROM codes"):
            self._to_id[(kind, value)] = i
            self._to_str[(kind, i)] = value
            self._next[kind] = max(self._next.get(kind, 0), i + 1)

    def id(self, kind: str, value: str) -> int:
        k = (kind, value)
        i = self._to_id.get(k)
        if i is None:
            i = self._next.get(kind, 0)
            if kind.startswith("f:") and i >= UNKNOWN:
                return UNKNOWN
            self._next[kind] = i + 1
            self._to_id[k] = i
            self._to_str[(kind, i)] = value
            self._pending.append((kind, value, i))
        return i

    def lookup(self, kind: str, value: str) -> int | None:
        return self._to_id.get((kind, value))

    def str(self, kind: str, i: int) -> str | None:
        return self._to_str.get((kind, i))

    def values(self, kind: str) -> dict[int, str]:
        return {i: v for (k, i), v in self._to_str.items() if k == kind}

    def flush(self) -> None:
        """Persist codes created since the last flush. Call inside the ingest transaction."""
        if self._pending:
            self.conn.executemany("INSERT OR IGNORE INTO codes(kind, value, id) VALUES (?,?,?)", self._pending)
            self._pending = []

    # ---- features
    def pack(self, features: dict) -> int:
        out = 0
        for i, f in enumerate(FEATURES):
            v = features.get(f)
            code = UNKNOWN if v is None else self.id(f"f:{f}", str(v))
            out |= code << (8 * i)
        return out

    def feature_value(self, feat: int, feature: str) -> str | None:
        i = FEATURES.index(feature)
        return self.str(f"f:{feature}", (feat >> (8 * i)) & 255)


class LeadIndex:
    """lead number -> packed features, per workflow code. Dense numpy arrays (lead ids are sequential
    per workflow); falls back to a dict for ids that don't parse."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.reload()

    def reload(self) -> None:
        self._arr: dict[int, np.ndarray] = {}
        self._extra: dict[tuple[int, str], int] = {}
        rows = self.conn.execute("SELECT wf, lead, feat FROM leads").fetchall()
        for wf, lead, feat in rows:
            self.put(wf, lead, feat)

    def put(self, wf: int, lead: int, feat: int) -> None:
        a = self._arr.get(wf)
        if a is None or lead >= len(a):
            size = max(1024, lead + 1, 0 if a is None else len(a) * 2)
            b = np.full(size, -1, dtype=np.int64)
            if a is not None:
                b[: len(a)] = a
            self._arr[wf] = a = b
        a[lead] = feat

    def get(self, wf: int, lead: int) -> int:
        a = self._arr.get(wf)
        if a is None or lead >= len(a):
            return -1
        return int(a[lead])
