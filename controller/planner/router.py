"""Bundling and routing: many Changes -> one config edit per workflow -> auto-apply or approval.

Routing policy (hybrid, see SUBMISSION.md §6):
  * auto-apply only if the workflow policy permits every changed dim AND the allocation shift is within
    max_shift AND no change is marked needs_approval (customer-facing promotions always go to a human);
  * a *safety* change that is auto-permitted but larger than max_shift is applied as the largest allowed
    step now (protective), and keeps stepping on later ticks while the evidence persists;
  * everything else goes into ONE approval request carrying every change, so a busy approver answers
    one question instead of five.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.configs import changed_dims, diff, limit_shift, max_shift
from ..governance.governor import Submission
from .base import Change, Ctx, candidate
from .impact import affected_leads, impact_score


@dataclass
class Routed:
    submission: Submission | None
    note: str


def route(ctx: Ctx, changes: list[Change], kind: str) -> Routed:
    if not changes:
        return Routed(None, "nothing to change")
    cfg = candidate(ctx.config, changes)
    dims = changed_dims(ctx.config, cfg)
    if not dims:
        return Routed(None, "changes cancel out")
    pol = ctx.meta.governance.auto_apply
    shift = max_shift(ctx.config, cfg)
    wants_human = any(c.needs_approval for c in changes)
    route_ = "approval"
    if pol.permits(dims) and not wants_human:
        if shift <= pol.max_shift + 1e-9:
            route_ = "auto"
        elif all(c.safety or c.kind in ("did_rebalance", "drop_arm", "explore") for c in changes):
            cfg = limit_shift(ctx.config, cfg, pol.max_shift)
            dims = changed_dims(ctx.config, cfg)
            route_ = "auto" if dims and max_shift(ctx.config, cfg) <= pol.max_shift + 1e-9 else "approval"
    summary = "; ".join(c.summary for c in changes)
    rationale = "\n".join(f"[{c.kind}] {c.rationale}" for c in changes)
    evidence: dict = {"changes": [{"kind": c.kind, "summary": c.summary, "evidence": c.evidence} for c in changes]}
    for c in changes:
        if "validation_run_id" in c.evidence:
            evidence["validation_run_id"] = c.evidence["validation_run_id"]
    intents = [i for c in changes for i in c.intents]
    sub = Submission(
        wf=ctx.wf, route=route_, kind=kind, base_version=ctx.version, config=cfg,
        summary=summary[:900], diff=diff(ctx.config, cfg), affected_leads=affected_leads(ctx, changes),
        dry_run={c.kind: c.dry_run for c in changes}, impact_score=impact_score(changes),
        rationale=rationale, evidence=evidence, intents=intents)
    return Routed(sub, f"{route_}: dims={dims} shift={shift:.3f}")
