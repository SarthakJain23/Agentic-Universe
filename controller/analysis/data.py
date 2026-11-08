"""SQL aggregations over the `calls`/`leads` facts. The only place analysis code touches SQL.

Every function returns small aggregates (hundreds to a few thousand rows), never raw calls, so the
analysis cost is independent of event volume apart from the indexed range scan.
"""
from __future__ import annotations

import sqlite3

import numpy as np

from ..ingest.codec import Codec

DIM_COL = {"welcome_message": "w", "voice": "v", "caller_id": "d", "prompt_version": "p"}
DIM_KIND = DIM_COL  # codec kind == column name
_HOUR = "(((minute % 1440) + 1440) % 1440) / 60"


class Data:
    def __init__(self, conn: sqlite3.Connection, codec: Codec):
        self.conn, self.codec = conn, codec

    def wfc(self, wf: str) -> int | None:
        return self.codec.lookup("wf", wf)

    def opt(self, dim: str, code: int) -> str | None:
        return self.codec.str(DIM_KIND[dim], code)

    def code(self, dim: str, option: str) -> int | None:
        return self.codec.lookup(DIM_KIND[dim], option)

    # ------------------------------------------------------------------ pickup by hour x features
    def hour_feat_pickup(self, wf: str, since: int, until: int) -> np.ndarray:
        """rows: feat, hour, dials, connected."""
        c = self.wfc(wf)
        if c is None:
            return np.zeros((0, 4), dtype=np.int64)
        rows = self.conn.execute(
            f"SELECT feat, {_HOUR} AS h, COUNT(*), SUM(connected) FROM calls WHERE wf=? AND minute>=? AND minute<?"
            " GROUP BY feat, h", (c, since, until)).fetchall()
        return np.array(rows, dtype=np.int64).reshape(-1, 4)

    def lead_feats(self, wf: str, since: int, until: int) -> np.ndarray:
        """rows: feat, count (lead uploads in window)."""
        c = self.wfc(wf)
        if c is None:
            return np.zeros((0, 2), dtype=np.int64)
        rows = self.conn.execute("SELECT feat, COUNT(*) FROM leads WHERE wf=? AND minute>=? AND minute<? GROUP BY feat",
                                 (c, since, until)).fetchall()
        return np.array(rows, dtype=np.int64).reshape(-1, 2)

    # ------------------------------------------------------------------ caller ID
    def did_strata(self, wf: str, since: int, until: int) -> list[tuple]:
        """rows: hour_bucket (minute//60), did code, dials, connected."""
        c = self.wfc(wf)
        if c is None:
            return []
        return self.conn.execute(
            "SELECT minute/60 AS b, d, COUNT(*), SUM(connected) FROM calls WHERE wf=? AND minute>=? AND minute<?"
            " GROUP BY b, d", (c, since, until)).fetchall()

    # ------------------------------------------------------------------ arms
    def arm_rows(self, wf: str, dim: str, since: int, until: int) -> np.ndarray:
        """Connected calls only. rows: cfg_version, cohort, feat, option code, n, engaged."""
        c = self.wfc(wf)
        if c is None:
            return np.zeros((0, 6), dtype=np.int64)
        col = DIM_COL[dim]
        rows = self.conn.execute(
            f"SELECT cfg_version, cohort, feat, {col}, COUNT(*), SUM(engaged) FROM calls"
            " WHERE wf=? AND minute>=? AND minute<? AND connected=1 GROUP BY cfg_version, cohort, feat, " + col,
            (c, since, until)).fetchall()
        return np.array(rows, dtype=np.int64).reshape(-1, 6)

    def arm_pickup_rows(self, wf: str, dim: str, since: int, until: int) -> np.ndarray:
        """All dials. rows: cfg_version, cohort, option code, dials, connected (guardrail check per arm)."""
        c = self.wfc(wf)
        if c is None:
            return np.zeros((0, 5), dtype=np.int64)
        col = DIM_COL[dim]
        rows = self.conn.execute(
            f"SELECT cfg_version, cohort, {col}, COUNT(*), SUM(connected) FROM calls"
            " WHERE wf=? AND minute>=? AND minute<? GROUP BY cfg_version, cohort, " + col,
            (c, since, until)).fetchall()
        return np.array(rows, dtype=np.int64).reshape(-1, 5)

    # ------------------------------------------------------------------ prompts / hallucination
    def prompt_stats(self, wf: str, since: int, until: int, prompt: str | None = None) -> dict[str, dict]:
        """{prompt: {connected, flags, engaged, dials, first, last}}"""
        c = self.wfc(wf)
        if c is None:
            return {}
        q = ("SELECT p, COUNT(*), SUM(connected), SUM(CASE WHEN connected=1 THEN flag ELSE 0 END), SUM(engaged),"
             " MIN(minute), MAX(minute) FROM calls WHERE wf=? AND minute>=? AND minute<?")
        args = [c, since, until]
        if prompt is not None:
            pc = self.code("prompt_version", prompt)
            if pc is None:
                return {}
            q += " AND p=?"
            args.append(pc)
        out = {}
        for p, dials, conn_, flags, eng, first, last in self.conn.execute(q + " GROUP BY p", args):
            out[self.opt("prompt_version", p)] = {"dials": dials, "connected": conn_ or 0, "flags": flags or 0,
                                                  "engaged": eng or 0, "first": first, "last": last}
        return out

    # ------------------------------------------------------------------ KPIs
    def kpis(self, wf: str, since: int, until: int) -> dict:
        c = self.wfc(wf)
        r = self.conn.execute("SELECT COUNT(*), SUM(connected), SUM(engaged), SUM(flag) FROM calls"
                              " WHERE wf=? AND minute>=? AND minute<?", (c, since, until)).fetchone() if c is not None \
            else (0, 0, 0, 0)
        dials, conn_, eng, flags = r[0] or 0, r[1] or 0, r[2] or 0, r[3] or 0
        return {"dials": dials, "connected": conn_, "engaged": eng, "flags": flags,
                "pickup_rate": conn_ / dials if dials else None,
                "engagement_rate": eng / conn_ if conn_ else None}

    def kpis_by_version(self, wf: str, since: int, until: int) -> list[tuple]:
        c = self.wfc(wf)
        if c is None:
            return []
        return self.conn.execute("SELECT cfg_version, COUNT(*), SUM(connected), SUM(engaged) FROM calls"
                                 " WHERE wf=? AND minute>=? AND minute<? GROUP BY cfg_version", (c, since, until)).fetchall()

    def active_leads(self, wf: str, now: int, campaign_days: int) -> np.ndarray:
        """Leads still in their cadence window and not concluded. rows: feat, count."""
        c = self.wfc(wf)
        if c is None:
            return np.zeros((0, 2), dtype=np.int64)
        start = (now // 1440 - campaign_days + 1) * 1440
        up = dict(self.conn.execute("SELECT feat, COUNT(*) FROM leads WHERE wf=? AND minute>=? GROUP BY feat",
                                    (c, start)).fetchall())
        done = dict(self.conn.execute("SELECT feat, COUNT(*) FROM calls WHERE wf=? AND minute>=? AND engaged=1"
                                      " GROUP BY feat", (c, start)).fetchall())
        rows = [(f, max(n - done.get(f, 0), 0)) for f, n in up.items()]
        return np.array(rows, dtype=np.int64).reshape(-1, 2)

    def first_minute(self, wf: str) -> int | None:
        c = self.wfc(wf)
        if c is None:
            return None
        return self.conn.execute("SELECT MIN(minute) FROM calls WHERE wf=?", (c,)).fetchone()[0]
