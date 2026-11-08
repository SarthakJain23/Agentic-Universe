"""Governance client (C9): the only code that changes a workflow's config.

State machine (persisted in `proposals.state`):

    approval route:  SUBMITTING -> PENDING -> APPROVED -> APPLYING -> APPLIED
                                     |           |            |
                                     |           +-> LAPSED   +-> STALE (409: config moved, e.g. supervisor)
                                     +-> REJECTED | EXPIRED
    auto route:      AUTO_SUBMITTING -> APPLIED | FORBIDDEN | STALE
    any send:        -> FAILED (4xx we caused; alerted)

Crash safety: the proposal row is committed *before* the HTTP call, so a crash leaves a SUBMITTING /
AUTO_SUBMITTING / APPLYING row. `reconcile()` resolves it against the simulator - by proposal_id in
GET /approvals, or proposal_id / approval_id in GET /config/history - and only re-sends if the world
shows it never happened. proposal_id is the simulator's idempotency key, so a duplicate send is
rejected with 409 rather than applied twice.

Learning-state transitions ride along as `intents` and are applied in the same transaction that marks a
proposal APPLIED - an arm that never went live never shows up as having been tried.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from ..clock import label, ts_to_minute
from ..domain.workflow import WorkflowMeta
from ..simclient import (AutoApplyForbidden, DuplicateProposal, InvalidRequest, Lapsed, NotApproved, SimClient,
                         SimError, StaleBase, Unavailable)
from ..store import repo as R
from ..store.repo import Proposal, Repo

log = logging.getLogger(__name__)


@dataclass
class Submission:
    wf: str
    route: str                 # approval | auto
    kind: str                  # routine | incident | safety
    base_version: int
    config: dict
    summary: str
    diff: dict
    affected_leads: int
    dry_run: dict
    impact_score: float
    rationale: str
    evidence: dict
    intents: list
    supersedes: str | None = None
    expires_in_hours: float | None = None


def apply_intents(repo: Repo, p: Proposal, now: int) -> None:
    """Learning-state transitions that ride on a proposal. Called inside the transaction that marks the
    proposal APPLIED, so state flips if and only if the change really went live."""
    for it in p.intents:
        t = it.get("type")
        if t == "arm":
            if it.get("status"):
                repo.set_arm(p.wf, it["dim"], it.get("segment", "*"), it["option"], it["status"], now, it.get("note", ""))
            else:
                repo.clear_arm(p.wf, it["dim"], it.get("segment", "*"), it["option"])
        elif t == "meta":
            repo.set_meta(it["key"], it["value"])
        elif t == "incident":
            inc = repo.incident(it["id"])
            if inc and inc["state"] not in ("resolved", "abandoned"):
                inc["state"] = it["state"]
                inc["data"]["candidate"] = it.get("candidate")
                inc["data"]["candidate_source"] = it.get("source")
                inc["data"]["verify_since"] = now
                repo.put_incident(inc)


class Governor:
    def __init__(self, sim: SimClient, repo: Repo, metas: dict[str, WorkflowMeta]):
        self.sim, self.repo, self.metas = sim, repo, metas
        self.on_applied: Callable[[Proposal, int], None] = lambda p, now: apply_intents(repo, p, now)
        self.on_closed: Callable[[Proposal, int], None] = lambda p, now: None

    # ------------------------------------------------------------------ submit
    def submit(self, sub: Submission, now: int) -> Proposal:
        n = self.repo.count_proposals(sub.wf, now)
        # Deterministic within this store; the store id keeps two stores (e.g. a stray second controller)
        # from ever colliding on the simulator's idempotency key.
        pid = f"{self.repo.store_id()}.{sub.wf}.{now}.{sub.kind}.{n}"
        body = {"proposal_id": pid, "workflow_id": sub.wf, "base_version": sub.base_version,
                "proposed_config": sub.config}
        if sub.route == "approval":
            body.update({"change_summary": sub.summary, "diff": sub.diff or {"note": "see summary"},
                         "affected_leads": int(sub.affected_leads), "dry_run": sub.dry_run or {"note": "n/a"},
                         "impact_score": round(min(max(float(sub.impact_score), 0.0), 1.0), 3),
                         "rationale": sub.rationale, "evidence": sub.evidence})
            if sub.expires_in_hours:
                body["expires_in_hours"] = sub.expires_in_hours
        else:
            body.update({"rationale": sub.rationale, "evidence": sub.evidence})
        # What we keep for audit: the full request incl. the human-facing fields even for auto-apply.
        request = {k: v for k, v in body.items() if k != "proposed_config"} | {
            "change_summary": sub.summary, "diff": sub.diff, "affected_leads": int(sub.affected_leads),
            "dry_run": sub.dry_run, "impact_score": sub.impact_score}
        p = Proposal(pid, sub.wf, sub.route, sub.kind,
                     R.SUBMITTING if sub.route == "approval" else R.AUTO_SUBMITTING, sub.base_version,
                     sub.config, request, sub.intents, None, None, now, None, None, None, sub.supersedes)
        with self.repo.tx():
            self.repo.add_proposal(p, now)
            self.repo.decide(sub.wf, now, f"submit_{sub.route}", sub.summary,
                             {"diff": sub.diff, "impact_score": sub.impact_score, "affected_leads": sub.affected_leads,
                              "dry_run": sub.dry_run, "rationale": sub.rationale, "evidence": sub.evidence,
                              "base_version": sub.base_version, "kind": sub.kind},
                             proposal_id=pid, dedup=f"submit:{pid}")
        self._send(p, body, now)
        return self.repo.proposal(pid)

    def _send(self, p: Proposal, body: dict, now: int) -> None:
        try:
            if p.route == "approval":
                resp = self.sim.request_approval(body)
                with self.repo.tx():
                    self.repo.update_proposal(p.proposal_id, now, state=R.PENDING, approval_id=resp["approval_id"])
                    self.repo.decide(p.wf, now, "approval_requested",
                                     f"{resp['approval_id']} pending (expires {resp.get('expires_at')})",
                                     {"approval": resp}, proposal_id=p.proposal_id, dedup=f"req:{p.proposal_id}")
            else:
                resp = self.sim.auto_apply(p.wf, body)
                self._mark_applied(p, resp["version"], now, via="auto-apply")
        except DuplicateProposal:
            self.reconcile_one(self.repo.proposal(p.proposal_id), now)
        except StaleBase as e:
            self._close(p, R.STALE, now, [f"base version {p.base_version} is stale: {e.detail}"])
        except AutoApplyForbidden as e:
            self._close(p, R.FORBIDDEN, now, e.detail if isinstance(e.detail, list) else [str(e.detail)])
        except InvalidRequest as e:
            self._close(p, R.FAILED, now, [str(e)])
            with self.repo.tx():
                self.repo.alert(now, "ticket", p.wf, f"proposal {p.proposal_id} refused by API: {e}",
                                dedup=f"failed:{p.proposal_id}")
        except Unavailable as e:
            log.warning("send of %s deferred (%s); will reconcile", p.proposal_id, e)

    # ------------------------------------------------------------------ reconcile
    def reconcile(self, now: int) -> None:
        for p in self.repo.proposals(states=R.IN_FLIGHT):
            try:
                self.reconcile_one(p, now)
            except Unavailable as e:
                log.warning("reconcile %s deferred: %s", p.proposal_id, e)

    def reconcile_one(self, p: Proposal, now: int) -> None:
        if p.state == R.SUBMITTING:
            found = next((a for a in self.sim.approvals(workflow_id=p.wf) if a["proposal_id"] == p.proposal_id), None)
            if found is None:
                self._resend_or_stale(p, now)
                return
            with self.repo.tx():
                self.repo.update_proposal(p.proposal_id, now, state=R.PENDING, approval_id=found["approval_id"])
            p = self.repo.proposal(p.proposal_id)
        if p.state == R.AUTO_SUBMITTING:
            v = self._version_with(p.wf, proposal_id=p.proposal_id)
            if v is not None:
                self._mark_applied(p, v, now, via="auto-apply (reconciled)")
            else:
                self._resend_or_stale(p, now)
            return
        if p.state in (R.PENDING, R.APPROVED, R.APPLYING):
            a = self.sim.approval(p.approval_id)
            st = a["status"]
            decided = ts_to_minute(a["decided_at"]) if a.get("decided_at") else None
            if st == "pending":
                return
            if st == "rejected":
                self._close(p, R.REJECTED, now, a.get("reasons") or [], decided)
            elif st == "expired":
                self._close(p, R.EXPIRED, now, ["approver did not decide before expiry"], ts_to_minute(a["expires_at"]))
            elif st == "lapsed":
                self._close(p, R.LAPSED, now, ["approved but not applied within the apply window"], decided)
            elif st == "applied":
                v = self._version_with(p.wf, approval_id=p.approval_id)
                self._mark_applied(p, v, now, via="approval (reconciled)", decided=decided)
            elif st == "approved":
                self._apply(p, now, decided)

    def _apply(self, p: Proposal, now: int, decided: int | None) -> None:
        with self.repo.tx():
            self.repo.update_proposal(p.proposal_id, now, state=R.APPLYING, decided_minute=decided)
            self.repo.decide(p.wf, now, "approved", f"{p.approval_id} approved at {label(decided or now)}",
                             {"approval_id": p.approval_id}, proposal_id=p.proposal_id, dedup=f"ok:{p.proposal_id}")
        try:
            resp = self.sim.apply(p.wf, p.approval_id)
            self._mark_applied(p, resp["version"], now, via="approval", decided=decided)
        except StaleBase as e:
            self._close(p, R.STALE, now, [f"config changed since proposal (now v{(e.detail or {}).get('current_version')})"],
                        decided)
        except Lapsed:
            self._close(p, R.LAPSED, now, ["apply window passed"], decided)
        except NotApproved:
            v = self._version_with(p.wf, approval_id=p.approval_id)
            if v is not None:
                self._mark_applied(p, v, now, via="approval (reconciled)", decided=decided)

    def _resend_or_stale(self, p: Proposal, now: int) -> None:
        cur = self.sim.config(p.wf)["version"]
        if cur != p.base_version:
            self._close(p, R.STALE, now, [f"never reached the simulator and config moved to v{cur}"])
            return
        body = {"proposal_id": p.proposal_id, "workflow_id": p.wf, "base_version": p.base_version,
                "proposed_config": p.config}
        keys = ("change_summary", "diff", "affected_leads", "dry_run", "impact_score", "rationale", "evidence",
                "expires_in_hours") if p.route == "approval" else ("rationale", "evidence")
        body.update({k: p.request[k] for k in keys if k in p.request})
        log.info("re-sending %s after restart", p.proposal_id)
        self._send(p, body, now)

    def _version_with(self, wf: str, proposal_id: str | None = None, approval_id: str | None = None) -> int | None:
        for v in self.sim.config_history(wf):
            if (proposal_id and v.get("proposal_id") == proposal_id) or (approval_id and v.get("approval_id") == approval_id):
                return v["version"]
        return None

    # ------------------------------------------------------------------ terminal transitions
    def _mark_applied(self, p: Proposal, version: int | None, now: int, via: str, decided: int | None = None) -> None:
        with self.repo.tx():
            fields = dict(state=R.APPLIED, applied_version=version, applied_minute=now)
            if decided is not None:
                fields["decided_minute"] = decided
            self.repo.update_proposal(p.proposal_id, now, **fields)
            fresh = self.repo.proposal(p.proposal_id)
            self.on_applied(fresh, now)
            self.repo.decide(p.wf, now, "applied", f"v{version} live via {via}: {p.request.get('change_summary')}",
                             {"version": version, "approval_id": p.approval_id, "diff": p.request.get("diff")},
                             proposal_id=p.proposal_id, dedup=f"applied:{p.proposal_id}")

    def _close(self, p: Proposal, state: str, now: int, reasons: list, decided: int | None = None) -> None:
        with self.repo.tx():
            fields = dict(state=state, reasons=reasons)
            if decided is not None:
                fields["decided_minute"] = decided
            self.repo.update_proposal(p.proposal_id, now, **fields)
            self.repo.decide(p.wf, now, state.lower(), f"{p.proposal_id} {state.lower()}: {'; '.join(map(str, reasons))}",
                             {"reasons": reasons, "approval_id": p.approval_id}, proposal_id=p.proposal_id,
                             dedup=f"{state}:{p.proposal_id}")
            self.on_closed(self.repo.proposal(p.proposal_id), now)

    # ------------------------------------------------------------------ budget helpers
    def in_flight(self, wf: str, route: str | None = None) -> list[Proposal]:
        return [p for p in self.repo.proposals(wf=wf, states=R.IN_FLIGHT) if route is None or p.route == route]

    def requests_used(self, wf: str, now: int) -> int:
        return self.repo.approval_requests_since(wf, now - 1440)


__all__ = ["Governor", "Submission", "SimError"]
