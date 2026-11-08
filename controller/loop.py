"""Main loop: drive simulated time, one controller step per advance. Safe to kill at any point.

Restart semantics: the store is reopened, every chunk after the last committed one is ingested, every
in-flight proposal is reconciled against the simulator, and the loop continues from the simulator's
current minute. Nothing is replayed from memory.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from .agent import Controller
from .clock import label
from .settings import SETTINGS
from .simclient import Finished, SimClient
from .store.db import open_for_run

log = logging.getLogger(__name__)


def fingerprint(sim: SimClient) -> str:
    """Identifies one simulator world: scenario + its history (deterministic per seed) + workflows."""
    st = sim.state()
    hist = sim.list_chunks("history")
    wfs = sorted(w["workflow_id"] for w in sim.workflows())
    raw = json.dumps([st["scenario"], st["end_minute"], [(c["chunk"], c["last_event_id"]) for c in hist], wfs])
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


_LOCK = None


def single_writer(db_path: str | Path) -> None:
    """Exclusive advisory lock next to the store: a second controller on the same store exits at once
    instead of racing the first (double proposals, stale applies)."""
    global _LOCK
    import fcntl
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _LOCK = open(str(db_path) + ".lock", "w")
    try:
        fcntl.flock(_LOCK, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another controller holds {db_path}.lock - refusing to run two writers")


def open_store(sim: SimClient, db_path: str | Path):
    single_writer(db_path)
    fp = fingerprint(sim)
    conn = open_for_run(db_path, fp)
    # Same scenario but a *restarted* simulator is a new world (the sim keeps state in memory): detect it
    # by our checkpoint being ahead of what the simulator has.
    last = conn.execute("SELECT MAX(chunk) FROM chunks").fetchone()[0]
    if last is not None and last >= sim.state()["chunks"]:
        conn.close()
        log.warning("store is ahead of the simulator - simulator was restarted; archiving old store")
        conn = open_for_run(db_path, fp + f"-reset-{int(time.time())}")
        conn.execute("UPDATE meta SET value=? WHERE key='fingerprint'", (json.dumps(fp),))
    return conn


def run(sim_url: str, db_path: str | Path, tick_minutes: int = SETTINGS.tick_minutes) -> None:
    sim = SimClient(sim_url)
    conn = open_store(sim, db_path)
    ctl = Controller(sim, conn)
    st = sim.state()
    log.info("controller up: scenario=%s at %s, store=%s", st["scenario"], label(st["sim_minute"]), db_path)
    t_start = time.perf_counter()
    ctl.step(st["sim_minute"])
    finished = st["finished"]
    while not finished:
        t0 = time.perf_counter()
        try:
            adv = sim.advance(tick_minutes)
        except Finished:
            break
        dt = time.perf_counter() - t0
        ctl.step(adv["sim_minute"], advance_secs=dt)
        finished = adv["finished"]
        if adv["sim_minute"] % 1440 == 0:
            log.info("%s done (wall %.0fs so far)", label(adv["sim_minute"] - 1), time.perf_counter() - t_start)
    ctl.step(sim.state()["sim_minute"])
    log.info("scenario finished in %.0fs wall", time.perf_counter() - t_start)
