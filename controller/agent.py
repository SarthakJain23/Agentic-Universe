"""The controller: one tick = ingest -> reconcile governance -> per workflow (sync, safety pass, routine
pass) -> metrics. Orchestration only; decisions live in planner/, statistics in analysis/ and stats/.
"""
from __future__ import annotations

import logging
import resource
import sys
import time

from .analysis.data import Data
from .clock import MIN_PER_DAY, in_hours, label, next_open, ts_to_minute
from .domain.workflow import WorkflowMeta
from .governance.governor import Governor
from .ingest.codec import Codec, LeadIndex
from .ingest.ingestor import Ingestor
from .planner import callerid, experiments, incident, schedule
from .planner.base import Change, Ctx
from .planner.incident import Credits
from .planner.router import route
from .settings import SETTINGS, Settings
from .simclient import SimClient, Unavailable
from .store import repo as R
from .store.repo import Proposal, Repo

log = logging.getLogger(__name__)


def _rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / (1024 * 1024) if sys.platform == "darwin" else r / 1024


class Controller:
    def __init__(self, sim: SimClient, conn, s: Settings = SETTINGS):
        self.sim, self.s = sim, s
        self.repo = Repo(conn)
        self.codec = Codec(conn)
        self.leads = LeadIndex(conn)
        self.ingestor = Ingestor(sim, conn, self.codec, self.leads)
        self.data = Data(conn, self.codec)
        self.metas = {w["workflow_id"]: WorkflowMeta.from_api(w) for w in sim.workflows()}
        self.gov = Governor(sim, self.repo, self.metas)
        self.credits = Credits(sim)
        self._cfg: dict[str, dict] = {}
        self._last_routine_eval: dict[str, int] = {}

    # ================================================================== tick
    def step(self, now: int, advance_secs: float = 0.0) -> None:
        t0 = time.perf_counter()
        ing = self.ingestor.catch_up()
        t1 = time.perf_counter()
        self.gov.reconcile(now)
        for wf in sorted(self.metas):
            try:
                self._tick_workflow(wf, now)
            except Unavailable as e:
                log.warning("%s: simulator unavailable this tick (%s); will retry next tick", wf, e)
        t2 = time.perf_counter()
        # Lag = how far our ingested data trails the simulator: zero when every chunk it has published is in.
        st = self.sim.state()
        ours = self.repo.conn.execute("SELECT MAX(chunk), MAX(until_minute) FROM chunks").fetchone()
        behind = st["chunks"] - 1 - (ours[0] if ours[0] is not None else -1)
        lag = 0 if behind <= 0 else now - (ours[1] if ours[1] is not None else now)
        with self.repo.tx():
            self.repo.metric({
                "minute": now, "wall": time.time(), "events": ing.events, "ingest_secs": round(t1 - t0, 3),
                "decide_secs": round(t2 - t1, 3), "advance_secs": round(advance_secs, 3),
                "lag_minutes": lag,
                "decisions": self.repo.conn.execute("SELECT COUNT(*) FROM decisions WHERE minute=?", (now,)).fetchone()[0],
                "credits": float(st["credits_remaining"]), "rss_mb": round(_rss_mb(), 1)})
            self._health_alerts(now, lag, float(st["credits_remaining"]))

    def _health_alerts(self, now: int, lag: int, credits: float) -> None:
        """Loop-level alerts for on-call. Page only when a human must act now; everything else is a ticket."""
        if lag > 120:
            self.repo.alert(now, "page", None, f"ingestion is {lag} sim-minutes behind the simulator: decisions are "
                            "stale. Check `controller status` ingest_secs and the simulator /events/chunks endpoint.",
                            dedup=f"lag:{now // 240}")
        if credits < self.s.credit_floor:
            self.repo.alert(now, "ticket", None, f"credits at {credits:.0f} (< floor {self.s.credit_floor:.0f}): only "
                            "incident work may spend now; top up before the next incident.", dedup="credits_floor")
        for inc in self.repo.open_incidents():
            if now - inc["opened_minute"] > 6 * 60:
                self.repo.alert(now, "page", inc["wf"], f"[{inc['wf']}] incident {inc['id']} ({inc['kind']} on "
                                f"{inc['subject']}) open for {(now - inc['opened_minute']) // 60}h, state {inc['state']}. "
                                f"Run `controller why {inc['wf']} --at \"{label(now)}\"` and check the approval queue.",
                                dedup=f"stuck:{inc['id']}")

    def _tick_workflow(self, wf: str, now: int) -> None:
        ctx = self._ctx(wf, now)
        self._incidents(ctx)
        ctx = self._ctx(wf, now)
        self._safety(ctx)
        ctx = self._ctx(wf, now)
        self._routine(ctx)

    # ================================================================== context + config mirror
    def _ctx(self, wf: str, now: int) -> Ctx:
        cur = self.sim.config(wf)
        if cur["version"] > self.repo.latest_version(wf):
            self._sync_history(wf, cur, now)
        return Ctx(now, self.metas[wf], cur["version"], cur["config"], self.data, self.repo, self.codec, self.s)

    def _sync_history(self, wf: str, cur: dict, now: int) -> None:
        known = self.repo.latest_version(wf)
        with self.repo.tx():
            for v in self.sim.config_history(wf):
                if v["version"] <= known:
                    continue
                if v["version"] == cur["version"]:
                    cfg = cur["config"]
                else:
                    p = self.repo.proposal(v["proposal_id"]) if v.get("proposal_id") else None
                    cfg = p.config if p else {"unknown": "intermediate version not observed"}
                applied = ts_to_minute(v["applied_at"]) if v.get("applied_at") else None
                self.repo.put_config_version(wf, v, cfg, applied)
                if v["actor"] == "supervisor":
                    self._on_supervisor_edit(wf, v, applied if applied is not None else now)
                elif v["actor"].startswith("controller") and not (v.get("proposal_id") and
                                                                  self.repo.proposal(v["proposal_id"])):
                    self.repo.alert(now, "page", wf, f"[{wf}] config v{v['version']} was applied by a controller "
                                    f"that is not this one (proposal {v.get('proposal_id')}). Two controllers may be "
                                    "driving the same workflow: stop one before it fights the other.",
                                    dedup=f"foreign:{wf}:{v['version']}")

    def _on_supervisor_edit(self, wf: str, v: dict, at: int) -> None:
        dims = v.get("changed_dims") or []
        until = at + self.s.supervisor_freeze_hours * 60
        freeze = self.repo.meta(f"freeze:{wf}", {}) or {}
        for d in dims:
            freeze[d] = max(freeze.get(d, 0), until)
        self.repo.set_meta(f"freeze:{wf}", freeze)
        self.repo.decide(wf, at, "supervisor_edit",
                         f"Supervisor changed {dims} (v{v['version']}). We respect it: no experiment changes on "
                         f"these dims until {label(until)}; pending proposals built on the old config will be "
                         "re-planned, not forced.", {"version": v["version"], "changed_dims": dims,
                                                     "frozen_until": until}, dedup=f"sup:{wf}:{v['version']}")
        self.repo.alert(at, "info", wf, f"[{wf}] supervisor edit v{v['version']} on {dims}", dedup=f"supa:{wf}:{v['version']}")

    # ================================================================== incidents (C8)
    def _incidents(self, ctx: Ctx) -> None:
        if not self.repo.open_incidents(ctx.wf, "hallucination"):
            incident.screen_prompt_change(ctx, self.credits, self.sim)
        with self.repo.tx():
            incident.detect(ctx)
            for inc in self.repo.open_incidents(ctx.wf, "hallucination"):
                incident.progress(ctx, inc, self.repo.proposal)
        for inc in self.repo.open_incidents(ctx.wf, "hallucination"):
            if inc["state"] != "open":
                continue
            # Crash between "proposal submitted" and "incident updated"? The proposal's intents name the
            # incident, so the link is recovered from the proposals table instead of re-proposing.
            linked = next((p for p in self.gov.in_flight(ctx.wf)
                           if any(i.get("type") == "incident" and i.get("id") == inc["id"] for i in p.intents)), None)
            if linked:
                inc["state"] = "fixing"
                inc["data"]["proposal_id"] = linked.proposal_id
                self.repo.put_incident(inc)
                continue
            if self.gov.requests_used(ctx.wf, ctx.now) >= ctx.meta.governance.max_requests_per_24h:
                self.repo.decide(ctx.wf, ctx.now, "incident_blocked", "approver request limit reached; fix "
                                 "will be proposed when a slot frees", {}, incident_id=inc["id"],
                                 dedup=f"blocked:{inc['id']}:{ctx.now // 240}")
                continue
            if not self._decidable(ctx):
                self.repo.decide(ctx.wf, ctx.now, "incident_deferred", "a request now would expire before the "
                                 "approver's next working window; proposing when it opens", {}, incident_id=inc["id"],
                                 dedup=f"defer:{inc['id']}:{ctx.now // MIN_PER_DAY}")
                continue
            change = incident.prepare_fix(ctx, inc, self.credits, self.sim)
            if change is None:
                self.repo.put_incident(inc)
                continue
            routed = route(ctx, [change], "incident")
            if routed.submission is None:
                continue
            p = self.gov.submit(routed.submission, ctx.now)
            inc["state"] = "fixing"
            inc["data"]["proposal_id"] = p.proposal_id
            inc["data"]["candidate"] = change.evidence.get("candidate")
            with self.repo.tx():
                fresh = self.repo.incident(inc["id"])
                if fresh["state"] == "verifying":   # applied synchronously (auto-apply)
                    continue
                self.repo.put_incident(inc)

    # ================================================================== safety pass (every tick)
    def _safety(self, ctx: Ctx) -> None:
        busy = {i.get("kind") for p in self.gov.in_flight(ctx.wf) for i in p.request.get("evidence", {}).get("changes", [])}
        # A human just said no to this kind of change: give them (and the data) time before asking again.
        cooldown = ctx.now - self.s.rejection_cooldown_minutes
        busy |= {i.get("kind") for p in self.repo.proposals(wf=ctx.wf, states=[R.REJECTED])
                 if (p.decided_minute or p.created_minute) >= cooldown
                 for i in p.request.get("evidence", {}).get("changes", [])}
        changes: list[Change] = []
        if "did_degraded" not in busy:
            changes += callerid.plan_safety(ctx)
        if "rollback" not in busy:
            changes += experiments.plan_decay(ctx)
        if not changes:
            return
        routed = route(ctx, changes, "safety")
        sub = routed.submission
        if sub is None:
            return
        if sub.route == "approval":
            pending = self.gov.in_flight(ctx.wf, "approval")
            if pending:
                # Anything already in the approver's queue will move the base version when it applies and
                # make this request stale - burning one of the day's limited requests for nothing. Wait for
                # it to resolve (at most the approver's latency), then ask on the fresh base.
                self.repo.decide(ctx.wf, ctx.now, "safety_waiting", f"{sub.summary} - waiting for "
                                 f"{pending[0].approval_id or pending[0].proposal_id} to resolve first",
                                 {}, dedup=f"swait:{ctx.wf}:{pending[0].proposal_id}")
                return
            if not self._decidable(ctx):
                return
            if self.gov.requests_used(ctx.wf, ctx.now) >= ctx.meta.governance.max_requests_per_24h:
                self.repo.decide(ctx.wf, ctx.now, "safety_blocked", f"{sub.summary} - approver limit reached",
                                 {}, dedup=f"sblocked:{ctx.wf}:{ctx.now // 240}")
                return
        self.gov.submit(sub, ctx.now)

    # ================================================================== routine pass (paced)
    def _routine(self, ctx: Ctx) -> None:
        s, gov = self.s, ctx.meta.governance
        last = self._last_routine_eval.get(ctx.wf)
        if last is not None and ctx.now - last < 120:
            return
        if self.gov.in_flight(ctx.wf, "approval"):
            return
        self._last_routine_eval[ctx.wf] = ctx.now
        changes = schedule.plan(ctx) + experiments.plan_routine(ctx) + callerid.plan_rebalance(ctx)
        if not changes:
            return
        routed = route(ctx, changes, "routine")
        sub = routed.submission
        if sub is None:
            return
        if sub.route == "approval" and not self._approval_slot_ok(ctx):
            auto_ok = [c for c in changes if not c.needs_approval and ctx.meta.governance.auto_apply.enabled]
            routed = route(ctx, auto_ok, "routine") if auto_ok else None
            if routed is None or routed.submission is None or routed.submission.route != "auto":
                return
            sub = routed.submission
        if sub.route == "auto" and not self._auto_slot_ok(ctx):
            return
        self.gov.submit(sub, ctx.now)

    def _decidable(self, ctx: Ctx) -> bool:
        """Would a request sent now be decided before it expires? The approver only decides inside
        approver_hours, after a latency of up to approver_latency_minutes[1]."""
        gov = ctx.meta.governance
        worst = next_open(next_open(ctx.now, gov.approver_hours) + gov.approver_latency_minutes[1], gov.approver_hours)
        return worst <= ctx.now + gov.proposal_ttl_hours * 60

    def _approval_slot_ok(self, ctx: Ctx) -> bool:
        s, gov, now = self.s, ctx.meta.governance, ctx.now
        if not in_hours(now, gov.approver_hours):
            return False
        quiet = s.tenant_quiet_until_hour.get(ctx.meta.tenant)
        if quiet is not None and (now % MIN_PER_DAY) < quiet * 60:
            return False
        # The decision must land today (compliance: "requests sent at night sit in a queue").
        if (now % MIN_PER_DAY) + gov.approver_latency_minutes[1] > gov.approver_hours[1] * 60:
            return False
        cap = min(max(gov.max_requests_per_24h - s.incident_reserve_requests, 1),
                  s.tenant_max_routine_requests.get(ctx.meta.tenant, s.max_routine_requests_per_day))
        if self.repo.conn.execute(
                "SELECT COUNT(*) FROM proposals WHERE wf=? AND route='approval' AND kind='routine' AND created_minute>?",
                (ctx.wf, now - MIN_PER_DAY)).fetchone()[0] >= cap:
            return False
        reserve = s.incident_reserve_requests if gov.max_requests_per_24h >= 2 else 0
        if self.gov.requests_used(ctx.wf, now) >= gov.max_requests_per_24h - reserve:
            return False
        last = self.repo.conn.execute("SELECT MAX(created_minute) FROM proposals WHERE wf=? AND route='approval'",
                                      (ctx.wf,)).fetchone()[0]
        return last is None or now - last >= s.min_request_spacing_minutes

    def _auto_slot_ok(self, ctx: Ctx) -> bool:
        last = self.repo.conn.execute(
            "SELECT MAX(created_minute) FROM proposals WHERE wf=? AND route='auto' AND kind='routine'",
            (ctx.wf,)).fetchone()[0]
        return last is None or ctx.now - last >= self.s.routine_auto_spacing_minutes
