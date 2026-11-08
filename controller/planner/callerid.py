"""Caller-ID component (C5): detect degraded numbers fast (safety), rebalance load slowly (routine)."""
from __future__ import annotations

from ..analysis import did_health as H
from ..domain.configs import dist, quantize, tv
from .base import Change, Ctx


def _health(ctx: Ctx, hours: int) -> dict[str, H.DidHealth]:
    rows = ctx.data.did_strata(ctx.wf, ctx.now - hours * 60, ctx.now)
    return H.assess(rows, lambda c: ctx.data.opt("caller_id", c))


def degraded(ctx: Ctx) -> tuple[list[str], dict]:
    """Spam flagging is a *change point*: a number's pickup ratio falls sharply against its own recent past.
    Load also lowers a number's ratio (health = base / (1 + (load/l0)^2)), but slowly (load decays over
    ~2 days), and the cure for load is rebalancing, not exclusion. So the safety trigger needs BOTH a low
    ratio now AND a significant drop versus the same number's previous 48h."""
    s = ctx.s
    cur = dist(ctx.config, "default", "caller_id")
    w = s.did_window_hours
    short = _health(ctx, w)
    prior_rows = ctx.data.did_strata(ctx.wf, ctx.now - w * 60 - 48 * 60, ctx.now - w * 60)
    prior = H.assess(prior_rows, lambda c: ctx.data.opt("caller_id", c))
    bad, drops = [], {}
    for d in cur:
        h, p = short.get(d), prior.get(d)
        if not h or h.dials < s.did_min_dials or h.ratio is None or h.ratio >= s.did_bad_ratio or h.p_low >= s.did_bad_p:
            continue
        if p is None or p.dials < s.did_min_dials or p.ratio is None:
            continue   # no history to compare with: rebalancing (not exclusion) handles never-measured numbers
        drop = h.ratio / p.ratio
        drops[d] = round(drop, 3)
        if drop < s.did_sudden_drop:
            bad.append(d)
    ev = {"window_6h": {d: h.as_dict() for d, h in short.items()},
          "prior_48h": {d: h.as_dict() for d, h in prior.items()}, "drop_vs_prior": drops}
    return bad, ev


def _target(ctx: Ctx, exclude: set[str]) -> tuple[dict, dict]:
    s = ctx.s
    health = _health(ctx, 72)
    cur = dist(ctx.config, "default", "caller_id")
    options = [o for o in ctx.meta.options["caller_id"] if o not in exclude]
    blocked = set(exclude) | {o for o, st in ctx.repo.arm_status(ctx.wf, "caller_id").items()
                              if st["status"] == "dropped"}
    options = [o for o in options if o not in blocked]
    target, why = H.target_distribution(health, options, {k: v for k, v in cur.items() if k in options},
                                        s.did_keep_ratio, s.did_min_dials, s.did_probe_share)
    target = quantize({k: v for k, v in target.items() if k not in blocked})
    return target, {"health_72h": {d: h.as_dict() for d, h in health.items()}, **why}


def plan_safety(ctx: Ctx) -> list[Change]:
    bad, ev = degraded(ctx)
    if not bad:
        return []
    target, why = _target(ctx, set(bad))
    if not target:
        return []
    cur = dist(ctx.config, "default", "caller_id")
    worst = min((ev["window_6h"].get(d) or {}).get("ratio") or 1 for d in bad)
    share = sum(cur.get(d, 0) for d in bad)

    def edit(cfg: dict) -> None:
        cfg["allocations"]["default"]["caller_id"] = target

    primary_pickup = ctx.meta.objective.primary == "pickup_rate"
    return [Change(
        kind="did_degraded", summary=f"Stop dialling from {', '.join(bad)}: pickup {worst:.0%} of the other numbers",
        rationale=("Caller IDs are assigned at random per dial, so in the same hours every number reaches the same "
                   f"people. {', '.join(bad)} connects significantly less often (ratio < {ctx.s.did_bad_ratio}, "
                   f"p < {ctx.s.did_bad_p}) and fell sharply against its own previous 48h - a change point consistent "
                   "with spam flagging, not gradual load. Traffic moves to healthy numbers."),
        edit=edit, evidence=ev | {"target": target, "why": why},
        dry_run={"method": "observed pickup ratio vs other numbers in the same hours",
                 "share_moved": round(share, 3), "expected_pickup_gain_on_moved_share": round(1 / worst - 1, 3)
                 if worst else None},
        impact=min(1.0, 0.4 + share * (1 - worst) * (2.0 if primary_pickup else 1.0)), safety=True,
        intents=[{"type": "arm", "dim": "caller_id", "segment": "*", "option": d, "status": "dropped",
                  "note": f"degraded ratio {worst:.2f}"} for d in bad])]


def plan_rebalance(ctx: Ctx) -> list[Change]:
    s = ctx.s
    last = ctx.repo.meta(f"did_rebalance_at:{ctx.wf}")
    if last is not None and ctx.now - last < 12 * 60:
        return []
    if ctx.is_frozen("caller_id"):
        return []
    target, why = _target(ctx, set())
    cur = dist(ctx.config, "default", "caller_id")
    if not target:
        return []
    # Damped update: move halfway. Health responds to load with a ~2-day lag, so jumping straight to the
    # ratio-weighted target overshoots and oscillates; halving the step converges.
    excluded = set(why.get("excluded", []))
    target = quantize({o: 0.5 * cur.get(o, 0.0) * (o not in excluded) + 0.5 * target.get(o, 0.0)
                       for o in set(cur) | set(target)})
    if tv(cur, target) < s.did_rebalance_min_tv:
        return []

    def edit(cfg: dict) -> None:
        cfg["allocations"]["default"]["caller_id"] = target

    return [Change(
        kind="did_rebalance", summary=f"Rebalance caller IDs to {target}",
        rationale=("Number health falls as a number's recent call volume rises, so spreading dials across healthy "
                   "numbers raises pickup. Shares follow each number's observed pickup ratio; never-used numbers "
                   f"get a {s.did_probe_share:.0%} probe each so a bad one costs little."),
        edit=edit, evidence=why | {"from": cur, "to": target},
        dry_run={"method": "pickup ratio per number vs the others, same hours, last 72h", **why},
        impact=0.1, intents=[{"type": "meta", "key": f"did_rebalance_at:{ctx.wf}", "value": ctx.now}])]
