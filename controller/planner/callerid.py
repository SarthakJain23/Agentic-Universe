"""Caller-ID component (C5): detect degraded numbers fast (safety), rebalance load slowly (routine)."""
from __future__ import annotations

from ..analysis import did_health as H
from ..domain.configs import dist, quantize, tv
from .base import Change, Ctx


def _health(ctx: Ctx, hours: int) -> dict[str, H.DidHealth]:
    rows = ctx.data.did_strata(ctx.wf, ctx.now - hours * 60, ctx.now)
    return H.assess(rows, lambda c: ctx.data.opt("caller_id", c))


def degraded(ctx: Ctx) -> tuple[list[str], dict]:
    """Spam flagging is a *change point*: a number suddenly connects far less often than the same other
    number it was dialling alongside. Load also lowers pickup, but gradually (load decays over ~2 days), and
    its cure is rebalancing, not exclusion.

    Test, pairwise and hour-stratified: number d is degraded if, against EVERY comparator c that has data
    in both windows, (a) d's pickup ratio vs c over the last 6h is low and significant, and (b) that ratio
    fell below `did_sudden_drop` x the same pair's ratio over the previous 48h. No comparator with history
    -> no safety trigger (rebalancing will still shift traffic by observed health)."""
    s = ctx.s
    cur = dist(ctx.config, "default", "caller_id")
    w = s.did_window_hours
    now_rows = ctx.data.did_strata(ctx.wf, ctx.now - w * 60, ctx.now)
    prior_rows = ctx.data.did_strata(ctx.wf, ctx.now - w * 60 - 48 * 60, ctx.now - w * 60)
    code = {d: ctx.data.code("caller_id", d) for d in ctx.meta.options["caller_id"]}

    def share(rows, d):
        tot = sum(r[2] for r in rows)
        return sum(r[2] for r in rows if r[1] == code.get(d)) / tot if tot else 0.0

    bad, pairs, surged = [], {}, {}
    for d in cur:
        # If this number's own share of dials jumped (usually because WE moved traffic onto it), a falling
        # ratio is explained by load we created - rebalance, don't exclude.
        s_now, s_old = share(now_rows, d), share(prior_rows, d)
        if s_old > 0 and s_now > 1.5 * s_old:
            surged[d] = {"share_6h": round(s_now, 3), "share_prior_48h": round(s_old, 3)}
            continue
        verdicts = []
        for c in cur:
            if c == d or code.get(d) is None or code.get(c) is None:
                continue
            r_now, p_now, n_now = H.pair_ratio(now_rows, code[d], code[c])
            r_old, _, n_old = H.pair_ratio(prior_rows, code[d], code[c])
            if r_now is None or r_old is None or n_now < s.did_min_dials or n_old < s.did_min_dials:
                continue
            hit = r_now < s.did_bad_ratio and p_now < s.did_bad_p and r_now / r_old < s.did_sudden_drop
            pairs[f"{d}_vs_{c}"] = {"ratio_6h": round(r_now, 3), "ratio_prior_48h": round(r_old, 3),
                                   "p_worse": round(p_now, 6), "dials": n_now, "flag": hit}
            verdicts.append(hit)
        if verdicts and all(verdicts):
            bad.append(d)
    ev = {"pairs": pairs, "share_surge_skipped": surged,
          "window_6h": {d: h.as_dict() for d, h in _health(ctx, w).items()}}
    return bad, ev


def _target(ctx: Ctx, exclude: set[str]) -> tuple[dict, dict]:
    s = ctx.s
    health = _health(ctx, 72)
    cur = dist(ctx.config, "default", "caller_id")
    options = [o for o in ctx.meta.options["caller_id"] if o not in exclude]
    statuses = ctx.repo.arm_status(ctx.wf, "caller_id")
    cooldown = s.did_cooldown_hours * 60
    blocked = set(exclude) | {o for o, st in statuses.items() if st["status"] == "dropped" or
                              (st["status"] == "cooldown" and ctx.now - st["since_minute"] < cooldown)}
    options = [o for o in options if o not in blocked]
    # A number back from cooldown is judged only on evidence gathered since it came back - its healthy
    # history from before the flag must not vouch for it - and is re-probed at a smaller share.
    reentry = {o: st["since_minute"] + cooldown for o, st in statuses.items() if st["status"] == "cooldown"
               and o in options}
    for o, since in reentry.items():
        fresh = H.assess(ctx.data.did_strata(ctx.wf, since, ctx.now), lambda c: ctx.data.opt("caller_id", c))
        if o in fresh:
            health[o] = fresh[o]
        else:
            health.pop(o, None)
    target, why = H.target_distribution(health, options, {k: v for k, v in cur.items() if k in options},
                                        s.did_keep_ratio, s.did_min_dials, s.did_probe_share)
    for o in reentry:
        if o in why.get("probing", []):
            target[o] = s.did_reprobe_share
    if reentry:
        why["reentry_after_cooldown"] = sorted(reentry)
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
        intents=[{"type": "arm", "dim": "caller_id", "segment": "*", "option": d, "status": "cooldown",
                  "note": f"degraded ratio {worst:.2f}; re-measured after {ctx.s.did_cooldown_hours}h"} for d in bad])]


def plan_rebalance(ctx: Ctx) -> list[Change]:
    s = ctx.s
    if ctx.is_frozen("caller_id"):
        return []
    # Normally at most every 12h (health reacts to load over days). Sooner when a number in use is far
    # behind the others right now - typically one we just overloaded.
    last = ctx.repo.meta(f"did_rebalance_at:{ctx.wf}")
    short = _health(ctx, s.did_window_hours)
    cur0 = dist(ctx.config, "default", "caller_id")
    urgent = any(h.ratio is not None and h.ratio < s.did_bad_ratio and h.dials >= s.did_min_dials
                 for d, h in short.items() if cur0.get(d, 0) > 0)
    if last is not None and ctx.now - last < (2 if urgent else 12) * 60:
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
