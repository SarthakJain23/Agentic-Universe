"""Event ingestion (C1): backlog-safe, idempotent, restartable.

Unit of work = one simulator chunk. Each chunk is streamed, parsed and written in ONE transaction that
also records the chunk as ingested. A crash mid-chunk rolls back to the previous chunk boundary; on
restart we list chunks after the last committed one and continue. `INSERT OR IGNORE` on event_id makes
a replayed chunk a no-op even if the checkpoint were somehow behind.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field

from ..clock import ts_to_minute
from ..settings import SETTINGS
from ..simclient import SimClient
from ..store.db import tx
from .codec import Codec, LeadIndex, id_number

log = logging.getLogger(__name__)

_CALL_SQL = ("INSERT OR IGNORE INTO calls(event_id, wf, minute, lead, person, feat, attempt, cfg_version, cohort,"
             " w, v, d, p, connected, engaged, flag, disp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
_LEAD_SQL = "INSERT OR IGNORE INTO leads(wf, lead, person, campaign, feat, minute) VALUES (?,?,?,?,?,?)"
_MISC_SQL = "INSERT OR IGNORE INTO sim_events(event_id, type, wf, minute, body) VALUES (?,?,?,?,?)"


@dataclass
class IngestStats:
    chunks: int = 0
    events: int = 0
    calls: int = 0
    leads: int = 0
    wall_secs: float = 0.0
    last_event_minute: int | None = None
    notable: list[dict] = field(default_factory=list)  # config_changed / approval_decided events


class Ingestor:
    def __init__(self, sim: SimClient, conn: sqlite3.Connection, codec: Codec, leads: LeadIndex):
        self.sim, self.conn, self.codec, self.leads = sim, conn, codec, leads

    def last_chunk(self) -> int:
        row = self.conn.execute("SELECT MAX(chunk) FROM chunks").fetchone()
        return -1 if row[0] is None else row[0]

    def catch_up(self) -> IngestStats:
        """Ingest every chunk (history first, then events) newer than the checkpoint, in order."""
        after = self.last_chunk()
        metas = self.sim.list_chunks("history", after) + self.sim.list_chunks("events", after)
        stats = IngestStats()
        for meta in sorted(metas, key=lambda m: m["chunk"]):
            self._ingest_chunk(meta, stats)
        return stats

    # ------------------------------------------------------------------------------------------
    def _ingest_chunk(self, meta: dict, stats: IngestStats) -> None:
        t0 = time.perf_counter()
        n_events = n_calls = n_leads = 0
        last_minute = None
        calls: list[tuple] = []
        leads: list[tuple] = []
        misc: list[tuple] = []
        cid, lix = self.codec, self.leads
        batch = SETTINGS.insert_batch
        try:
            with tx(self.conn):
                for raw in self.sim.iter_chunk_lines(meta["chunk"]):
                    if not raw:
                        continue
                    ev = json.loads(raw)
                    n_events += 1
                    typ = ev["type"]
                    minute = ts_to_minute(ev["ts"])
                    last_minute = minute
                    if typ == "call":
                        wf = cid.id("wf", ev["workflow_id"])
                        lead = id_number(ev["lead_id"])
                        var = ev["variant"]
                        qa = ev.get("qa") or {}
                        calls.append((
                            ev["event_id"], wf, minute, lead, id_number(ev["person_id"]), lix.get(wf, lead),
                            ev["attempt_no"], ev["config_version"], cid.id("cohort", ev["cohort_id"]),
                            cid.id("w", var["welcome_message"]), cid.id("v", var["voice"]),
                            cid.id("d", var["caller_id"]), cid.id("p", var["prompt_version"]),
                            1 if ev["connected"] else 0, 1 if ev["engaged"] else 0,
                            1 if qa.get("hallucination_flag") else 0, cid.id("disp", ev["disposition"])))
                        n_calls += 1
                        if len(calls) >= batch:
                            self.conn.executemany(_CALL_SQL, calls)
                            calls.clear()
                    elif typ == "lead":
                        wf = cid.id("wf", ev["workflow_id"])
                        lead = id_number(ev["lead_id"])
                        feat = cid.pack(ev["features"])
                        lix.put(wf, lead, feat)
                        leads.append((wf, lead, id_number(ev["person_id"]), id_number(ev["campaign_id"]) or 0,
                                      feat, minute))
                        n_leads += 1
                        if len(leads) >= batch:
                            self.conn.executemany(_LEAD_SQL, leads)
                            leads.clear()
                    else:
                        misc.append((ev["event_id"], typ, ev.get("workflow_id"), minute,
                                     raw.decode() if isinstance(raw, bytes) else raw))
                        if typ in ("config_changed", "approval_decided"):
                            stats.notable.append(ev)
                if calls:
                    self.conn.executemany(_CALL_SQL, calls)
                if leads:
                    self.conn.executemany(_LEAD_SQL, leads)
                if misc:
                    self.conn.executemany(_MISC_SQL, misc)
                cid.flush()
                wall = time.perf_counter() - t0
                self.conn.execute(
                    "INSERT OR REPLACE INTO chunks(chunk, kind, events, last_event_id, until_minute, ingested_wall,"
                    " wall_secs) VALUES (?,?,?,?,?,?,?)",
                    (meta["chunk"], meta["kind"], n_events, meta["last_event_id"], meta["until_minute"],
                     time.time(), wall))
        except BaseException:
            # In-memory caches may hold rows that were rolled back: rebuild them from the store.
            self.codec.reload()
            self.leads.reload()
            raise
        stats.chunks += 1
        stats.events += n_events
        stats.calls += n_calls
        stats.leads += n_leads
        stats.wall_secs += time.perf_counter() - t0
        stats.last_event_minute = last_minute if last_minute is not None else stats.last_event_minute
        log.info("ingested chunk %d (%s): %d events in %.2fs", meta["chunk"], meta["kind"], n_events,
                 time.perf_counter() - t0)
