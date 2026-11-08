"""Audit queries over our own store (C12). The simulator's logs are never consulted.

  why(wf, at)        - why did workflow X change around time T, who approved it, what happened next
  heard(person, day) - what a customer heard on a day, and the decision that made each variant live
"""
from __future__ import annotations

import json
import sqlite3

from .clock import MIN_PER_DAY, label, parse_label
from .ingest.codec import Codec, id_number
from .store.repo import Repo

CHANGE_DIMS_COL = {"welcome_message": "w", "voice": "v", "caller_id": "d", "prompt_version": "p"}


def _kpi(conn: sqlite3.Connection, wfc: int, a: int, b: int) -> dict:
    r = conn.execute("SELECT COUNT(*), SUM(connected), SUM(engaged), SUM(flag) FROM calls WHERE wf=? AND minute>=? AND "
                     "minute<?", (wfc, a, b)).fetchone()
    d, c, e, f = r[0] or 0, r[1] or 0, r[2] or 0, r[3] or 0
    return {"dials": d, "pickup": round(c / d, 4) if d else None, "engagement": round(e / c, 4) if c else None,
            "flag_rate": round(f / c, 4) if c else None}


def why(conn: sqlite3.Connection, wf: str, at: str) -> str:
    repo, codec = Repo(conn), Codec(conn)
    t = parse_label(at)
    versions = repo.config_versions(wf)
    if not versions:
        return f"No config history recorded for {wf}."
    near = [v for v in versions if v["applied_minute"] is not None and abs(v["applied_minute"] - t) <= 60]
    if near:
        v = min(near, key=lambda v: abs(v["applied_minute"] - t))
        intro = f"{wf} changed at {label(v['applied_minute'])} (v{v['version']})."
    else:
        prior = [v for v in versions if v["applied_minute"] is None or v["applied_minute"] <= t]
        v = prior[-1]
        intro = (f"No change to {wf} within 60 min of {label(t)}. The config live then was v{v['version']} "
                 f"(since {label(v['applied_minute']) if v['applied_minute'] is not None else 'before D0'}).")
    L = [f"# Why: {wf} @ {label(t)}", "", intro, "",
         f"- actor: **{v['actor']}**", f"- changed dims: {', '.join(v['changed_dims']) or '-'}"]
    p = repo.proposal(v["proposal_id"]) if v.get("proposal_id") else None
    if p:
        req = p.request
        L += [f"- proposal: `{p.proposal_id}` ({p.route}, {p.kind}) created {label(p.created_minute)}",
              f"- summary: {req.get('change_summary')}",
              f"- impact score: {req.get('impact_score')} · affected leads: {req.get('affected_leads')}"]
        if p.route == "approval":
            L.append(f"- approved by: the {wf} client approver, approval `{p.approval_id}`, decided "
                     f"{label(p.decided_minute) if p.decided_minute is not None else '?'}, applied {label(p.applied_minute)}")
        else:
            L.append(f"- approved by: workflow auto-apply policy (no human); applied {label(p.applied_minute)}")
        L += ["", "## Rationale", "", str(req.get("rationale")), "", "## Diff", "", "```json",
              json.dumps(req.get("diff"), indent=1)[:3000], "```", "", "## Evidence at decision time", "", "```json",
              json.dumps(req.get("evidence"), indent=1, default=str)[:4000], "```", "", "## Dry run", "", "```json",
              json.dumps(req.get("dry_run"), indent=1, default=str)[:2500], "```"]
    elif v["actor"] == "supervisor":
        sup = repo.decisions(wf, since=(v["applied_minute"] or t) - 1, until=(v["applied_minute"] or t) + 1,
                             kinds=["supervisor_edit"])
        L += ["- approved by: client supervisor edit (outside governance, by design)"]
        L += [f"- controller response: {d['summary']}" for d in sup]
    wfc = codec.lookup("wf", wf)
    if wfc is not None and v["applied_minute"] is not None:
        a = v["applied_minute"]
        nxt = next((x for x in versions if x["version"] == v["version"] + 1), None)
        end = min(a + MIN_PER_DAY, nxt["applied_minute"] if nxt and nxt["applied_minute"] else a + MIN_PER_DAY)
        L += ["", "## What happened next", "",
              f"- 24h before: {_kpi(conn, wfc, a - MIN_PER_DAY, a)}",
              f"- after (until {label(end)}): {_kpi(conn, wfc, a, end)}"]
        if nxt:
            L.append(f"- next change: v{nxt['version']} by {nxt['actor']} at "
                     f"{label(nxt['applied_minute']) if nxt['applied_minute'] is not None else '?'} ({', '.join(nxt['changed_dims'])})")
        later = repo.decisions(wf, since=a + 1, until=a + 2 * MIN_PER_DAY,
                               kinds=["applied", "rollback", "incident_opened", "incident_resolved", "rejected",
                                      "expired", "stale", "supervisor_edit"])
        for d in later[:12]:
            L.append(f"- {label(d['minute'])} {d['kind']}: {d['summary'][:200]}")
    return "\n".join(L)


def heard(conn: sqlite3.Connection, ident: str, day: int) -> str:
    repo, codec = Repo(conn), Codec(conn)
    wf = ident.split(":", 1)[0]
    wfc, num = codec.lookup("wf", wf), id_number(ident)
    if wfc is None or num is None:
        return f"Unknown id {ident!r} (expected e.g. nw_renewal:p0004567 or nw_renewal:l00012345)."
    col = "person" if ":p" in ident else "lead"
    rows = conn.execute(f"SELECT minute, w, v, d, p, cfg_version, cohort, connected, engaged, disp, attempt FROM calls "
                        f"WHERE wf=? AND {col}=? AND minute>=? AND minute<? ORDER BY minute",
                        (wfc, num, day * MIN_PER_DAY, (day + 1) * MIN_PER_DAY)).fetchall()
    if not rows:
        return f"No calls to {ident} on D{day}."
    versions = repo.config_versions(wf)
    L = [f"# What {ident} heard on D{day}", ""]
    for minute, w, v, d, p, ver, coh, conn_, eng, disp, att in rows:
        var = {"welcome_message": codec.str("w", w), "voice": codec.str("v", v), "caller_id": codec.str("d", d),
               "prompt_version": codec.str("p", p)}
        L.append(f"## {label(minute)} attempt {att}: {codec.str('disp', disp)} "
                 f"(config v{ver}, cohort {codec.str('cohort', coh)})")
        L.append(f"variant: {var}")
        for dim, val in var.items():
            src = _introduced(versions, ver, dim)
            if src is None:
                L.append(f"- {dim}={val}: business default config (v1)")
                continue
            p_ = repo.proposal(src["proposal_id"]) if src.get("proposal_id") else None
            why_ = p_.request.get("change_summary") if p_ else ("client supervisor edit" if src["actor"] == "supervisor"
                                                                 else src["actor"])
            who = (f"approval {p_.approval_id}" if p_ and p_.approval_id else
                   "auto-apply policy" if p_ else src["actor"])
            L.append(f"- {dim}={val}: allocation set in v{src['version']} at "
                     f"{label(src['applied_minute']) if src['applied_minute'] is not None else '?'} by {who}: {why_}")
        L.append("")
    return "\n".join(L)


def _introduced(versions: list[dict], ver: int, dim: str) -> dict | None:
    """The latest version <= ver that changed `dim` (None = business default)."""
    for v in reversed([x for x in versions if x["version"] <= ver]):
        if dim in v["changed_dims"]:
            return v
    return None
