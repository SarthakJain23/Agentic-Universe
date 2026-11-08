"""Hallucination incident response (C8): detect -> pick candidate -> validate -> propose -> verify.

Incident states:  open -> fixing (proposal in flight) -> verifying (fix live) -> resolved
                    ^            |  rejected / expired / stale            |  fix failed verification
                    +------------+----------------------------------------+

Candidate choice is deterministic and evidence-first:
  1. a prompt we have *observed* in production on this workflow with a normal flag rate (known-good,
     usually the one that ran before the incident) - no credits needed to choose it;
  2. only if none exists, ask the LLM for suggestions (3 credits) and take them in the order returned.
Every candidate must pass a validation run (required by the approver for any prompt change). Validation
measures safety only, so after the fix is live we verify BOTH that flags fall back to baseline AND that
engagement did not drop (the "compliance script read word for word" failure in memo 04).
"""
from __future__ import annotations

import logging

from ..analysis import hallucination as HL
from ..clock import label
from ..simclient import OutOfCredits, SimClient, SimError
from ..stats import SPRT, stratified_compare
from .base import Change, Ctx

log = logging.getLogger(__name__)
DAY = 1440


class Credits:
    """Credit ledger + guard. Non-incident work may not push the balance under the floor."""

    def __init__(self, sim: SimClient):
        self.sim = sim

    def remaining(self) -> float:
        return float(self.sim.state()["credits_remaining"])

    def can_spend(self, cost: float, incident: bool, floor: float) -> bool:
        rem = self.remaining()
        return rem >= cost and (incident or rem - cost >= floor)


def prompt_live_since(ctx: Ctx, prompt: str) -> int:
    """Minute the current prompt went live (from our config mirror), or the start of our records."""
    since = None
    for v in ctx.repo.config_versions(ctx.wf):
        if v["config"].get("prompt_version") == prompt:
            if since is None:
                since = v["applied_minute"] if v["applied_minute"] is not None else -10**9
        else:
            since = None
    first = ctx.data.first_minute(ctx.wf) or 0
    return max(since if since is not None else first, first)


def baseline(ctx: Ctx) -> float:
    b = ctx.repo.meta(f"halluc_baseline:{ctx.wf}")
    if b is None:
        st = ctx.data.prompt_stats(ctx.wf, -10**9, 0)
        flags = sum(v["flags"] for v in st.values())
        conn = sum(v["connected"] for v in st.values())
        b = HL.baseline_rate(flags, conn, ctx.s)
        if conn:
            ctx.repo.set_meta(f"halluc_baseline:{ctx.wf}", b)
    return b


def screen_prompt_change(ctx: Ctx, credits: Credits, sim: SimClient) -> dict | None:
    """A prompt we did not choose just went live (supervisor edit). Production data needs ~250 connected
    calls to convict it; one validation run (2 credits) answers in zero sim-minutes using the approver's
    own bar. This is the CTO's "spend credits where they change a decision": it moves time-to-fix from
    hours to the approver's latency."""
    prompt = ctx.config["prompt_version"]
    screened = ctx.repo.meta(f"screened:{ctx.wf}", {}) or {}
    if screened.get(prompt) is not None:
        return None
    versions = ctx.repo.config_versions(ctx.wf)
    last = next((v for v in reversed(versions) if "prompt_version" in v["changed_dims"]), None)
    stats = ctx.data.prompt_stats(ctx.wf, -10**9, ctx.now).get(prompt, {})
    if last is None or last["actor"] != "supervisor" or HL.healthy(stats, baseline(ctx), ctx.s):
        screened[prompt] = "known"
        ctx.repo.set_meta(f"screened:{ctx.wf}", screened)
        return None
    if not credits.can_spend(-(-ctx.s.validation_n // 100), incident=False, floor=ctx.s.credit_floor):
        return None
    try:
        v = sim.validate(ctx.wf, prompt, ctx.s.validation_n)
    except SimError as e:
        log.warning("screening validation failed: %s", e)
        return None
    ctx.repo.spend(ctx.wf, ctx.now, "validation_run", -(-ctx.s.validation_n // 100), v["validation_run_id"],
                   v.get("credits_remaining"))
    screened[prompt] = v["pass_rate"]
    ctx.repo.set_meta(f"screened:{ctx.wf}", screened)
    bar = ctx.meta.governance.min_validation_pass_rate
    ctx.repo.decide(ctx.wf, ctx.now, "prompt_screened", f"Supervisor prompt {prompt}: validation pass rate "
                    f"{v['pass_rate']:.3f} (approval bar {bar})", v, dedup=f"screen:{ctx.wf}:{prompt}:{last['version']}")
    if v["pass_rate"] >= bar:
        return None
    reason = (f"supervisor prompt {prompt} fails validation: pass rate {v['pass_rate']:.3f} on n={v['n']} "
              f"< approval bar {bar}")
    inc = {"id": f"inc.{ctx.wf}.halluc.{ctx.now}", "wf": ctx.wf, "kind": "hallucination", "subject": prompt,
           "state": "open", "opened_minute": ctx.now,
           "data": {"detection": {"baseline_rate": baseline(ctx), "validation": v}, "reason": reason, "tried": [],
                    "prompt_live_since": prompt_live_since(ctx, prompt)}}
    ctx.repo.put_incident(inc)
    ctx.repo.decide(ctx.wf, ctx.now, "incident_opened", f"Hallucination risk on prompt {prompt}: {reason}",
                    inc["data"]["detection"], incident_id=inc["id"], dedup=f"open:{inc['id']}")
    ctx.repo.alert(ctx.now, "page", ctx.wf, f"[{ctx.wf}] {reason}. Controller is proposing a validated rollback; "
                   "check the approval queue.", dedup=f"page:{inc['id']}")
    return inc


def detect(ctx: Ctx) -> dict | None:
    """Open a hallucination incident if the current prompt's flag rate is above baseline."""
    prompt = ctx.config["prompt_version"]
    if any(i["subject"] == prompt for i in ctx.repo.open_incidents(ctx.wf, "hallucination")):
        return None
    s = ctx.s
    since = prompt_live_since(ctx, prompt)
    win = ctx.data.prompt_stats(ctx.wf, max(since, ctx.now - s.halluc_window_hours * 60), ctx.now, prompt).get(prompt, {})
    chg = ctx.data.prompt_stats(ctx.wf, since, ctx.now, prompt).get(prompt, {})
    chk = HL.check(win, chg, baseline(ctx), s)
    if not chk.triggered:
        return None
    inc = {"id": f"inc.{ctx.wf}.halluc.{ctx.now}", "wf": ctx.wf, "kind": "hallucination", "subject": prompt,
           "state": "open", "opened_minute": ctx.now,
           "data": {"detection": chk.evidence, "reason": chk.reason, "tried": [], "prompt_live_since": since}}
    ctx.repo.put_incident(inc)
    ctx.repo.decide(ctx.wf, ctx.now, "incident_opened", f"Hallucination spike on prompt {prompt}: {chk.reason}",
                    chk.evidence, incident_id=inc["id"], dedup=f"open:{inc['id']}")
    ctx.repo.alert(ctx.now, "page", ctx.wf, f"[{ctx.wf}] hallucination flags up on {prompt}: {chk.reason}. "
                   "Controller is preparing a validated rollback; check the approval queue.", dedup=f"page:{inc['id']}")
    return inc


def candidates(ctx: Ctx, inc: dict) -> list[tuple[str, str]]:
    """[(prompt, source)] in preference order, excluding the bad prompt and ones already tried."""
    bad, tried = inc["subject"], set(inc["data"].get("tried", []))
    base = baseline(ctx)
    stats = ctx.data.prompt_stats(ctx.wf, -10**9, ctx.now)
    good = [(p, v) for p, v in stats.items()
            if p != bad and p not in tried and p in ctx.meta.options["prompt_version"] and HL.healthy(v, base, ctx.s)]
    good.sort(key=lambda pv: -pv[1]["last"])
    out = [(p, "known_good") for p, _ in good]
    out += [(p, "llm") for p in inc["data"].get("llm_suggestions", []) if p not in tried and p != bad
            and p not in [x for x, _ in out]]
    return out


def prepare_fix(ctx: Ctx, inc: dict, credits: Credits, sim: SimClient) -> Change | None:
    """Pick + validate a candidate. Returns a prompt-fix Change, or None (incident state updated)."""
    s, data = ctx.s, inc["data"]
    for _ in range(4):
        cands = candidates(ctx, inc)
        if not cands and "llm_suggestions" not in data:
            if not credits.can_spend(3, incident=True, floor=0):
                _note(ctx, inc, "no known-good prompt and no credits for LLM suggestions")
                return None
            try:
                resp = sim.suggest_fixes(ctx.wf, f"Hallucination flag rate spiked on prompt {inc['subject']}: "
                                                 f"{data.get('reason')}. Suggest safer prompt versions.")
            except (OutOfCredits, SimError) as e:
                _note(ctx, inc, f"LLM suggest failed: {e}")
                return None
            data["llm_suggestions"] = [x["prompt_version"] for x in resp.get("suggestions", [])]
            data["llm_summaries"] = {x["prompt_version"]: x.get("summary") for x in resp.get("suggestions", [])}
            ctx.repo.spend(ctx.wf, ctx.now, "llm_suggest_fixes", 3, inc["id"], resp.get("credits_remaining"))
            ctx.repo.decide(ctx.wf, ctx.now, "llm_suggestions", f"LLM suggested {data['llm_suggestions']} "
                            "(candidates only; validation and production evidence decide)",
                            {"suggestions": resp.get("suggestions")}, incident_id=inc["id"])
            continue
        if not cands:
            _note(ctx, inc, "all candidates exhausted")
            return None
        prompt, source = cands[0]
        val = _validate(ctx, inc, prompt, credits, sim)
        if val is None:
            return None
        if val["pass_rate"] >= ctx.meta.governance.min_validation_pass_rate:
            data.setdefault("validations", {})[prompt] = val
            return _fix_change(ctx, inc, prompt, source, val)
        data["tried"].append(prompt)
        ctx.repo.decide(ctx.wf, ctx.now, "candidate_rejected", f"{prompt} failed validation "
                        f"({val['pass_rate']:.3f} < {ctx.meta.governance.min_validation_pass_rate})",
                        val, incident_id=inc["id"])
    return None


def _validate(ctx: Ctx, inc: dict, prompt: str, credits: Credits, sim: SimClient) -> dict | None:
    s = ctx.s
    bar = ctx.meta.governance.min_validation_pass_rate
    best = None
    for n in (s.validation_n, s.validation_n_borderline):
        cost = -(-n // 100)
        if not credits.can_spend(cost, incident=True, floor=0):
            _note(ctx, inc, f"cannot afford validation of {prompt} (n={n})")
            return best
        try:
            v = sim.validate(ctx.wf, prompt, n)
        except (OutOfCredits, SimError) as e:
            _note(ctx, inc, f"validation failed: {e}")
            return best
        ctx.repo.spend(ctx.wf, ctx.now, "validation_run", cost, v["validation_run_id"], v.get("credits_remaining"))
        ctx.repo.decide(ctx.wf, ctx.now, "validation_run", f"{prompt}: pass rate {v['pass_rate']:.3f} on n={n}",
                        v, incident_id=inc["id"])
        best = v
        # Borderline (within 1pp of the bar on the small run): buy a bigger sample instead of a coin flip.
        if abs(v["pass_rate"] - bar) > 0.01:
            break
    return best


def _fix_change(ctx: Ctx, inc: dict, prompt: str, source: str, val: dict) -> Change:
    bad = inc["subject"]
    stats = ctx.data.prompt_stats(ctx.wf, -10**9, ctx.now)
    det = inc["data"]["detection"]

    def edit(cfg: dict) -> None:
        cfg["prompt_version"] = prompt

    sup = _last_actor(ctx, bad)
    reverts = f" (reverts the {sup} change to {bad})" if sup == "supervisor" else ""
    return Change(
        kind="prompt_fix", summary=f"Switch prompt {bad} -> {prompt}{reverts}: hallucination flags "
                                   f"{_rate(det)} vs baseline {det['baseline_rate']:.1%}",
        rationale=(f"{inc['data']['reason']}. Candidate {prompt} chosen because it is "
                   + ("a prompt that already ran on this workflow with a normal flag rate"
                      if source == "known_good" else "an LLM suggestion") +
                   f"; validation pass rate {val['pass_rate']:.3f} on n={val['n']}. After it goes live we verify "
                   "flags return to baseline and engagement does not drop."),
        edit=edit, evidence={"validation_run_id": val["validation_run_id"], "detection": det, "source": source,
                             "candidate": prompt},
        dry_run={"method": "production record of the candidate prompt on this workflow + validation run",
                 "candidate_history": stats.get(prompt), "bad_prompt_since_live": stats.get(bad),
                 "validation": val},
        impact=0.95, customer_facing=True, safety=True,
        intents=[{"type": "incident", "id": inc["id"], "state": "verifying", "candidate": prompt}])


def progress(ctx: Ctx, inc: dict, proposals) -> None:
    """Advance an open incident given its proposals' fate (called every tick)."""
    data = inc["data"]
    cur_prompt = ctx.config["prompt_version"]
    if inc["state"] == "fixing":
        p = proposals(data.get("proposal_id")) if data.get("proposal_id") else None
        if p is None or p.state in ("REJECTED", "EXPIRED", "LAPSED", "STALE", "FAILED", "FORBIDDEN"):
            reasons = (p.reasons or []) if p else []
            if any("validation" in str(r) for r in reasons):
                data["tried"].append(data.get("candidate"))
            inc["state"] = "open"
            _note(ctx, inc, f"fix proposal {data.get('proposal_id')} {p.state if p else 'missing'}: {reasons}; re-planning")
    if inc["state"] in ("open", "fixing") and cur_prompt != inc["subject"]:
        # The bad prompt is no longer live (supervisor reverted, or our fix applied): verify what is live now.
        inc["state"] = "verifying"
        data["candidate"] = cur_prompt
        data["verify_since"] = ctx.now
    if inc["state"] == "verifying":
        _verify(ctx, inc)
    ctx.repo.put_incident(inc)


def _verify(ctx: Ctx, inc: dict) -> None:
    s, data = ctx.s, inc["data"]
    live = ctx.config["prompt_version"]
    since = data.get("verify_since") or ctx.now
    st = ctx.data.prompt_stats(ctx.wf, since, ctx.now, live).get(live, {})
    n, k = st.get("connected", 0), st.get("flags", 0)
    base = baseline(ctx)
    sprt = SPRT(base, min(base * s.halluc_sprt_h1_ratio, 0.5), 0.01, 0.01)
    verdict = sprt.decide(k, n) if n else "continue"
    if verdict == "h1" and n >= 30:
        data["tried"].append(live)
        inc["state"] = "open"
        inc["subject"] = live
        _note(ctx, inc, f"fix {live} still hallucinating ({k}/{n}); trying next candidate")
        return
    # Engagement check: the fix must not trade hallucinations for a script nobody stays on.
    pre = ctx.data.prompt_stats(ctx.wf, data.get("prompt_live_since", 0) - 3 * DAY,
                                data.get("prompt_live_since", 0), None)
    pre_n = sum(v["connected"] for p, v in pre.items() if p != inc["subject"])
    pre_k = sum(v["engaged"] for p, v in pre.items() if p != inc["subject"])
    if n >= 200 and pre_n >= 200:
        c = stratified_compare([(n, st.get("engaged", 0), pre_n, pre_k)])
        data["engagement_check"] = c.as_dict()
        if c.lift < -s.fix_engagement_max_drop and c.p_worse >= 0.95:
            data["tried"].append(live)
            inc["state"] = "open"
            inc["subject"] = live
            _note(ctx, inc, f"fix {live} cut engagement {c.lift:+.1%}; trying next candidate")
            return
    if verdict == "h0" and n >= 200 and "engagement_check" in data:
        inc["state"] = "resolved"
        inc["resolved_minute"] = ctx.now
        ctx.repo.decide(ctx.wf, ctx.now, "incident_resolved",
                        f"{live} verified: flags {k}/{n} back at baseline, engagement {data['engagement_check']['lift']:+.1%} "
                        f"vs pre-incident; time to fix {(ctx.now - inc['opened_minute'])} min",
                        {"flags": k, "connected": n, "engagement": data["engagement_check"]}, incident_id=inc["id"],
                        dedup=f"resolved:{inc['id']}")


def _note(ctx: Ctx, inc: dict, msg: str) -> None:
    ctx.repo.decide(ctx.wf, ctx.now, "incident_note", msg, {"state": inc["state"]}, incident_id=inc["id"])
    log.info("[%s] %s %s: %s", label(ctx.now), ctx.wf, inc["id"], msg)


def _rate(det: dict) -> str:
    for k in ("window", "sprt"):
        w = det.get(k) or {}
        if w.get("connected"):
            return f"{w['flags'] / w['connected']:.1%}"
    if det.get("validation"):
        return f"validation pass rate {det['validation']['pass_rate']:.3f}"
    return "?"


def _last_actor(ctx: Ctx, prompt: str) -> str | None:
    for v in reversed(ctx.repo.config_versions(ctx.wf)):
        if v["config"].get("prompt_version") == prompt and "prompt_version" in v["changed_dims"]:
            return v["actor"]
    return None
