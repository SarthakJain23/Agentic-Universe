"""Typed accessors over the store. All writes that belong together happen inside one `tx()`.

The audit trail (`decisions`) is append-only. `dedup_key` makes recording idempotent: replaying a tick
after a crash does not duplicate an audit row.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

from .db import get_meta, set_meta, tx

# Proposal states (see governance/governor.py for the transitions).
SUBMITTING, PENDING, APPROVED, APPLYING, APPLIED = "SUBMITTING", "PENDING", "APPROVED", "APPLYING", "APPLIED"
REJECTED, EXPIRED, LAPSED, STALE, FAILED = "REJECTED", "EXPIRED", "LAPSED", "STALE", "FAILED"
AUTO_SUBMITTING, FORBIDDEN = "AUTO_SUBMITTING", "FORBIDDEN"
IN_FLIGHT = (SUBMITTING, PENDING, APPROVED, APPLYING, AUTO_SUBMITTING)
TERMINAL = (APPLIED, REJECTED, EXPIRED, LAPSED, STALE, FAILED, FORBIDDEN)


def _j(x: Any) -> str:
    return json.dumps(x, sort_keys=True, default=str)


@dataclass
class Proposal:
    proposal_id: str
    wf: str
    route: str
    kind: str
    state: str
    base_version: int
    config: dict
    request: dict
    intents: list
    approval_id: str | None
    reasons: list | None
    created_minute: int
    decided_minute: int | None
    applied_minute: int | None
    applied_version: int | None
    supersedes: str | None

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Proposal":
        return cls(r["proposal_id"], r["wf"], r["route"], r["kind"], r["state"], r["base_version"],
                   json.loads(r["config"]), json.loads(r["request"]), json.loads(r["intents"]), r["approval_id"],
                   json.loads(r["reasons"]) if r["reasons"] else None, r["created_minute"], r["decided_minute"],
                   r["applied_minute"], r["applied_version"], r["supersedes"])


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def tx(self):
        return tx(self.conn)

    # ------------------------------------------------------------------ meta
    def meta(self, key: str, default=None):
        return get_meta(self.conn, key, default)

    def set_meta(self, key: str, value) -> None:
        set_meta(self.conn, key, value)

    # ------------------------------------------------------------------ audit
    def decide(self, wf: str | None, minute: int, kind: str, summary: str, evidence: dict | None = None,
               proposal_id: str | None = None, incident_id: str | None = None, dedup: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO decisions(wf, minute, kind, summary, evidence, proposal_id, incident_id, dedup_key)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (wf, minute, kind, summary, _j(evidence or {}), proposal_id, incident_id, dedup))

    def decisions(self, wf: str | None = None, since: int | None = None, until: int | None = None,
                  kinds: Iterable[str] | None = None, limit: int = 500) -> list[dict]:
        q, args = "SELECT * FROM decisions WHERE 1=1", []
        if wf:
            q += " AND wf=?"; args.append(wf)
        if since is not None:
            q += " AND minute>=?"; args.append(since)
        if until is not None:
            q += " AND minute<=?"; args.append(until)
        if kinds:
            kinds = list(kinds)
            q += f" AND kind IN ({','.join('?' * len(kinds))})"; args += kinds
        q += " ORDER BY id LIMIT ?"
        args.append(limit)
        return [dict(r) | {"evidence": json.loads(r["evidence"])} for r in self.conn.execute(q, args)]

    def alert(self, minute: int, severity: str, wf: str | None, message: str, dedup: str | None = None) -> None:
        self.conn.execute("INSERT OR IGNORE INTO alerts(minute, severity, wf, message, dedup_key) VALUES (?,?,?,?,?)",
                          (minute, severity, wf, message, dedup))

    # ------------------------------------------------------------------ proposals
    def add_proposal(self, p: Proposal, now: int) -> None:
        self.conn.execute(
            "INSERT INTO proposals(proposal_id, wf, route, kind, state, base_version, config, request, intents,"
            " approval_id, reasons, created_minute, decided_minute, applied_minute, applied_version, supersedes,"
            " updated_minute) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.proposal_id, p.wf, p.route, p.kind, p.state, p.base_version, _j(p.config), _j(p.request),
             _j(p.intents), p.approval_id, _j(p.reasons) if p.reasons else None, p.created_minute,
             p.decided_minute, p.applied_minute, p.applied_version, p.supersedes, now))

    def update_proposal(self, pid: str, now: int, **fields) -> None:
        if "reasons" in fields and fields["reasons"] is not None:
            fields["reasons"] = _j(fields["reasons"])
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE proposals SET {sets}, updated_minute=? WHERE proposal_id=?",
                          (*fields.values(), now, pid))

    def proposal(self, pid: str) -> Proposal | None:
        r = self.conn.execute("SELECT * FROM proposals WHERE proposal_id=?", (pid,)).fetchone()
        return Proposal.from_row(r) if r else None

    def proposals(self, wf: str | None = None, states: Iterable[str] | None = None,
                  since: int | None = None) -> list[Proposal]:
        q, args = "SELECT * FROM proposals WHERE 1=1", []
        if wf:
            q += " AND wf=?"; args.append(wf)
        if states:
            states = list(states)
            q += f" AND state IN ({','.join('?' * len(states))})"; args += states
        if since is not None:
            q += " AND created_minute>=?"; args.append(since)
        return [Proposal.from_row(r) for r in self.conn.execute(q + " ORDER BY created_minute, proposal_id", args)]

    def approval_requests_since(self, wf: str, since: int) -> int:
        """Requests the approver has seen (or may have seen) in the window - what fatigue counts."""
        return self.conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE wf=? AND route='approval' AND created_minute>? AND state != ?",
            (wf, since, FAILED)).fetchone()[0]

    def count_proposals(self, wf: str, minute: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM proposals WHERE wf=? AND created_minute=?",
                                 (wf, minute)).fetchone()[0]

    # ------------------------------------------------------------------ config mirror
    def config_version(self, wf: str, version: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM config_versions WHERE wf=? AND version=?", (wf, version)).fetchone()
        return None if r is None else dict(r) | {"config": json.loads(r["config"]),
                                                 "changed_dims": json.loads(r["changed_dims"])}

    def config_versions(self, wf: str) -> list[dict]:
        return [dict(r) | {"config": json.loads(r["config"]), "changed_dims": json.loads(r["changed_dims"])}
                for r in self.conn.execute("SELECT * FROM config_versions WHERE wf=? ORDER BY version", (wf,))]

    def latest_version(self, wf: str) -> int:
        r = self.conn.execute("SELECT MAX(version) FROM config_versions WHERE wf=?", (wf,)).fetchone()
        return r[0] or 0

    def put_config_version(self, wf: str, v: dict, config: dict, applied_minute: int | None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO config_versions(wf, version, actor, config, changed_dims, applied_minute,"
            " proposal_id, approval_id) VALUES (?,?,?,?,?,?,?,?)",
            (wf, v["version"], v["actor"], _j(config), _j(v.get("changed_dims") or []), applied_minute,
             v.get("proposal_id"), v.get("approval_id")))

    # ------------------------------------------------------------------ arms
    def arm_status(self, wf: str, dim: str, segment: str = "*") -> dict[str, dict]:
        return {r["option"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM arms WHERE wf=? AND dim=? AND segment=?", (wf, dim, segment))}

    def set_arm(self, wf: str, dim: str, segment: str, option: str, status: str, minute: int, note: str = "") -> None:
        self.conn.execute(
            "INSERT INTO arms(wf, dim, segment, option, status, since_minute, note) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(wf, dim, segment, option) DO UPDATE SET status=excluded.status,"
            " since_minute=excluded.since_minute, note=excluded.note",
            (wf, dim, segment, option, status, minute, note))

    def clear_arm(self, wf: str, dim: str, segment: str, option: str) -> None:
        self.conn.execute("DELETE FROM arms WHERE wf=? AND dim=? AND segment=? AND option=?", (wf, dim, segment, option))

    # ------------------------------------------------------------------ incidents
    def incident(self, iid: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM incidents WHERE id=?", (iid,)).fetchone()
        return None if r is None else dict(r) | {"data": json.loads(r["data"])}

    def open_incidents(self, wf: str | None = None, kind: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM incidents WHERE state NOT IN ('resolved','abandoned')", []
        if wf:
            q += " AND wf=?"; args.append(wf)
        if kind:
            q += " AND kind=?"; args.append(kind)
        return [dict(r) | {"data": json.loads(r["data"])} for r in self.conn.execute(q + " ORDER BY opened_minute", args)]

    def incidents(self, wf: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM incidents", []
        if wf:
            q += " WHERE wf=?"; args.append(wf)
        return [dict(r) | {"data": json.loads(r["data"])} for r in self.conn.execute(q + " ORDER BY opened_minute", args)]

    def put_incident(self, inc: dict) -> None:
        self.conn.execute(
            "INSERT INTO incidents(id, wf, kind, subject, state, opened_minute, resolved_minute, data)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,"
            " resolved_minute=excluded.resolved_minute, data=excluded.data",
            (inc["id"], inc["wf"], inc["kind"], inc["subject"], inc["state"], inc["opened_minute"],
             inc.get("resolved_minute"), _j(inc.get("data") or {})))

    # ------------------------------------------------------------------ credits / metrics
    def spend(self, wf: str, minute: int, what: str, cost: float, ref: str | None, remaining: float | None) -> None:
        self.conn.execute("INSERT INTO credits_ledger(wf, minute, what, cost, ref, remaining) VALUES (?,?,?,?,?,?)",
                          (wf, minute, what, cost, ref, remaining))

    def metric(self, row: dict) -> None:
        cols = ",".join(row)
        self.conn.execute(f"INSERT OR REPLACE INTO metrics({cols}) VALUES ({','.join('?' * len(row))})",
                          tuple(row.values()))
