"""Post-run invariants for crash-recovery evidence. Reads the simulator API and our store.

  python scripts/verify_run.py --sim-url http://127.0.0.1:8730 --db state/crash.db

Checks:
  1. no double apply: every proposal_id appears at most once in every workflow's config history;
  2. no lost approval: no approval is left 'approved' (approved but never applied) at the end;
  3. every approval the simulator knows about is in our store, with the same approval_id;
  4. our store agrees with the world: every APPLIED proposal is in config history at that version;
  5. ingestion complete: our last ingested chunk == the simulator's last chunk; no duplicate events.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter

import httpx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim-url", required=True)
    ap.add_argument("--db", required=True)
    a = ap.parse_args()
    sim = httpx.Client(base_url=a.sim_url, timeout=60)
    db = sqlite3.connect(a.db)
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + msg)
        ok &= cond

    wfs = [w["workflow_id"] for w in sim.get("/workflows").json()]
    approvals = sim.get("/approvals").json()
    ours = {r[0]: r for r in db.execute("SELECT proposal_id, state, approval_id, applied_version, wf FROM proposals")}
    for wf in wfs:
        hist = sim.get(f"/workflows/{wf}/config/history").json()
        pids = Counter(v["proposal_id"] for v in hist if v["proposal_id"])
        dup = [p for p, n in pids.items() if n > 1]
        check(not dup, f"{wf}: no proposal applied twice ({len(pids)} controller versions){' DUP ' + str(dup) if dup else ''}")
        by_pid = {v["proposal_id"]: v["version"] for v in hist if v["proposal_id"]}
        mism = [pid for pid, r in ours.items() if r[4] == wf and r[1] == "APPLIED" and by_pid.get(pid) != r[3]]
        check(not mism, f"{wf}: every APPLIED proposal in our store is live in config history at that version")
        missing = [pid for pid in by_pid if pid not in ours]
        check(not missing, f"{wf}: every controller version in the world is known to our store")
    # An approval may legitimately stay 'approved' if applying it was refused because the config moved
    # (supervisor edit -> 409 stale) or the window passed; it is lost only if our store has no such record.
    closed = {r[2]: r[1] for r in ours.values() if r[2]}
    stuck = [x["approval_id"] for x in approvals if x["status"] == "approved"
             and closed.get(x["approval_id"]) not in ("STALE", "LAPSED")]
    explained = [x["approval_id"] for x in approvals if x["status"] == "approved" and x["approval_id"] not in stuck]
    check(not stuck, f"no approval lost: every approved request was applied or closed with a reason "
                     f"{'(stale/lapsed, not forced: ' + str(explained) + ')' if explained else ''}{stuck if stuck else ''}")
    unknown = [x["approval_id"] for x in approvals if x["proposal_id"] not in ours or ours[x["proposal_id"]][2] != x["approval_id"]]
    check(not unknown, f"all {len(approvals)} approvals are tracked in our store with matching approval_id")
    failed = db.execute("SELECT COUNT(*) FROM proposals WHERE state='FAILED'").fetchone()[0]
    check(failed == 0, f"no proposal refused by the API as invalid ({failed} FAILED)")
    st = sim.get("/sim/state").json()
    last = db.execute("SELECT MAX(chunk) FROM chunks").fetchone()[0]
    check(last == st["chunks"] - 1, f"ingested every chunk (ours {last}, simulator {st['chunks'] - 1})")
    n_calls = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    n_distinct = db.execute("SELECT COUNT(DISTINCT event_id) FROM calls").fetchone()[0]
    check(n_calls == n_distinct, f"no duplicate call events ({n_calls:,} calls)")
    check(st["finished"], f"scenario finished ({st['ts']})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
