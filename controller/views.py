"""Supervisor view (C11) and loop status (C14), rendered from the store.

The supervisor view is one screen per workflow, in the order Anita (memo 04) asked for: what's being
tested, what's winning, what we need from you, what changed today. Alerts appear once, not fifty times.
"""
from __future__ import annotations

import json
import sqlite3

from .analysis.data import Data
from .analysis.experiments import ArmTable
from .clock import MIN_PER_DAY, label, parse_label
from .domain.configs import EXPERIMENT_DIMS, round_dist
from .ingest.codec import Codec
from .store import repo as R
from .store.repo import Repo


def _now(conn: sqlite3.Connection, at: str | None) -> int:
    if at:
        return parse_label(at)
    m = conn.execute("SELECT MAX(minute) FROM metrics").fetchone()[0]
    return m or 0


def supervisor_view(conn: sqlite3.Connection, wf: str | None = None, at: str | None = None) -> str:
    repo, codec = Repo(conn), Codec(conn)
    data = Data(conn, codec)
    now = _now(conn, at)
    wfs = [wf] if wf else sorted(v for v in codec.values("wf").values())
    out = [f"# Supervisor view - {label(now)}", ""]
    for w in wfs:
        versions = [v for v in repo.config_versions(w) if v["applied_minute"] is None or v["applied_minute"] <= now]
        if not versions:
            continue
        cur = versions[-1]
        cfg = cur["config"]
        out += [f"## {w}  (config v{cur['version']}, prompt {cfg.get('prompt_version')})", ""]
        # -- being tested / winning
        out.append("**Being tested / winning**")
        for dim in EXPERIMENT_DIMS:
            d = (cfg.get("allocations", {}).get("default") or {}).get(dim) or {}
            arms = repo.arm_status(w, dim)
            table = ArmTable.build(data.arm_rows(w, dim, now - 2 * MIN_PER_DAY, now))
            champ = max(d, key=lambda o: d[o]) if d else None
            parts = []
            for o, share in sorted(d.items(), key=lambda kv: -kv[1]):
                code = data.code(dim, o)
                n, k = table.totals(code) if code is not None else (0, 0)
                tag = arms.get(o, {}).get("status") or ("champion" if o == champ else "testing")
                parts.append(f"{o} {share:.0%} [{tag}] eng {k / n:.1%} (n={n})" if n else f"{o} {share:.0%} [{tag}]")
            closed = [f"{o}: {a['status']}" for o, a in arms.items() if a["status"] in ("dropped", "retired")]
            out.append(f"- {dim}: " + " · ".join(parts) + (f"  — closed: {', '.join(closed)}" if closed else ""))
        did = (cfg.get("allocations", {}).get("default") or {}).get("caller_id") or {}
        out.append(f"- caller IDs: {round_dist(did, 2)}")
        out.append(f"- cohorts: {[c['id'] for c in cfg.get('cohorts') or []] or 'none'}")
        # -- waiting for you
        pend = [p for p in repo.proposals(wf=w, states=[R.PENDING, R.APPROVED, R.SUBMITTING])
                if p.created_minute <= now]
        out += ["", "**Waiting for you**"]
        out += [f"- `{p.approval_id}` (impact {p.request.get('impact_score')}, {p.request.get('affected_leads')} leads): "
                f"{p.request.get('change_summary')}" for p in sorted(pend, key=lambda p: -float(p.request.get('impact_score') or 0))] \
            or ["- nothing"]
        # -- changed today
        day0 = now - now % MIN_PER_DAY
        today = [v for v in versions if v["applied_minute"] is not None and v["applied_minute"] >= day0]
        out += ["", "**Changed today**"]
        for v in today:
            p = repo.proposal(v["proposal_id"]) if v.get("proposal_id") else None
            what = p.request.get("change_summary") if p else "supervisor edit" if v["actor"] == "supervisor" else v["actor"]
            out.append(f"- {label(v['applied_minute'])} v{v['version']} ({v['actor']}): {what}")
        if not today:
            out.append("- nothing")
        # -- incidents + alerts
        incs = [i for i in repo.incidents(w) if i["opened_minute"] <= now and
                (i["state"] not in ("resolved", "abandoned") or (i["resolved_minute"] or 0) >= day0)]
        if incs:
            out += ["", "**Incidents**"]
            out += [f"- {i['kind']} on {i['subject']} opened {label(i['opened_minute'])}: "
                    + (f"resolved {label(i['resolved_minute'])}" if i["resolved_minute"] and i["resolved_minute"] <= now
                       else "in progress") for i in incs]
        out.append("")
    return "\n".join(out)


def status(conn: sqlite3.Connection) -> str:
    m = conn.execute("SELECT COUNT(*), SUM(events), SUM(ingest_secs), SUM(decide_secs), SUM(advance_secs), MAX(rss_mb),"
                     " MIN(minute), MAX(minute), MAX(lag_minutes), SUM(decisions), MIN(credits), MAX(credits) FROM metrics"
                     ).fetchone()
    ticks, events, ing, dec, adv, rss, m0, m1, lag, decisions, cmin, cmax = m
    if not ticks:
        return "no metrics yet"
    sim_days = max((m1 - m0) / MIN_PER_DAY, 1e-9)
    wall = (ing or 0) + (dec or 0) + (adv or 0)
    p95 = conn.execute("SELECT decide_secs FROM metrics ORDER BY decide_secs DESC LIMIT 1 OFFSET ?",
                       (max(int(ticks * 0.05), 0),)).fetchone()
    L = ["# Controller status", "",
         f"- sim time covered: {label(m0)} -> {label(m1)} ({sim_days:.1f} days, {ticks} ticks)",
         f"- events ingested: {events:,} at {events / max(ing, 1e-9):,.0f} events/s (ingest wall {ing:.1f}s)",
         f"- wall per sim-day: {wall / sim_days:.1f}s (ingest {ing / sim_days:.1f}s, decide {dec / sim_days:.1f}s, "
         f"simulator advance {adv / sim_days:.1f}s)",
         f"- decision step: mean {dec / ticks:.2f}s, p95 {p95[0] if p95 else 0:.2f}s per tick",
         f"- max ingest lag: {lag} sim-minutes behind the simulator clock",
         f"- audit rows: {decisions} ({decisions / (sim_days * 24):.1f}/sim-hour)",
         f"- credits: {cmax:.0f} -> {cmin:.0f}",
         f"- peak RSS: {rss:.0f} MB", ""]
    by = conn.execute("SELECT state, COUNT(*) FROM proposals GROUP BY state").fetchall()
    L.append("- proposals: " + ", ".join(f"{s}={n}" for s, n in by))
    L += ["", "## Alerts", ""]
    for sev in ("page", "ticket", "info"):
        rows = conn.execute("SELECT minute, wf, message FROM alerts WHERE severity=? ORDER BY minute", (sev,)).fetchall()
        L.append(f"### {sev} ({len(rows)})")
        L += [f"- {label(r[0])} {r[2]}" for r in rows[-15:]] or ["- none"]
    return "\n".join(L)


def dump_json(obj) -> str:
    return json.dumps(obj, indent=1, default=str)
