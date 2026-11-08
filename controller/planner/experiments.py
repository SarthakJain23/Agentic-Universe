"""Experiment component for welcome_message and voice (C6 design, C7 promotion / decay / rollback).

Arm state is *derived from the live config* plus a few persisted facts (which option is the holdout,
which were dropped or retired). So a supervisor edit, a rejected proposal or a crash can never leave
the registry claiming an arm ran when it did not ("only what actually happened counts as evidence").

Lifecycle of one dimension:
  explore  - champion keeps the bulk; up to N untested options get `explore_share` each
  adapt    - an arm that is probably worse (P>=0.8, n>=150) is dropped; probably better (P>=0.9) is raised
  promote  - Holm-significant winner with n>=400 per arm -> winner 90%, old champion 10% holdout
  watch    - winner vs holdout on a trailing 24h window; if the winner now probably loses -> rollback
"""
from __future__ import annotations

from ..analysis.experiments import ArmTable, evaluate
from ..domain.configs import EXPERIMENT_DIMS, dist, quantize
from ..stats import stratified_compare
from .base import Change, Ctx

DAY = 1440


def _code(ctx: Ctx, dim: str):
    return lambda o: ctx.data.code(dim, o)


def arm_state(ctx: Ctx, dim: str) -> dict:
    cur = dist(ctx.config, "default", dim)
    statuses = ctx.repo.arm_status(ctx.wf, dim)
    closed = {o for o, st in statuses.items() if st["status"] in ("dropped", "retired")}
    # Arms we closed but that still carry traffic (an auto-applied step of a larger move): never champion,
    # keep draining them until they reach zero.
    draining = sorted(o for o in cur if o in closed)
    holdout = next((o for o, st in statuses.items() if st["status"] == "holdout" and cur.get(o, 0) > 0), None)
    pool = [o for o in cur if o != holdout and o not in closed] or [o for o in cur if o not in closed] or list(cur)
    champion = max(pool, key=lambda o: (cur[o], o))
    challengers = sorted(o for o in cur if o not in (champion, holdout) and o not in closed)
    untested = [o for o in ctx.meta.options[dim] if o not in cur and o not in closed]
    return {"dist": cur, "champion": champion, "holdout": holdout, "challengers": challengers,
            "closed": sorted(closed), "draining": draining, "untested": untested}


def enabled(ctx: Ctx) -> bool:
    return ctx.meta.objective.primary == "engagement_rate" or ctx.s.experiment_on_pickup_primary


def _guardrail_ok(ctx: Ctx, dim: str, arm: str, ref: str, since: int) -> tuple[bool, dict]:
    """Per-arm guardrail: the arm must not be significantly worse on the guardrail KPI (randomised, so
    the comparison is causal). For engagement-primary workflows the guardrail is pickup."""
    if ctx.meta.objective.guardrail != "pickup_rate":
        return True, {}
    rows = ctx.data.arm_pickup_rows(ctx.wf, dim, since, ctx.now)
    a, b = ctx.data.code(dim, arm), ctx.data.code(dim, ref)
    strata: dict = {}
    for ver, coh, opt, n, k in rows:
        cell = strata.setdefault((int(ver), int(coh)), [0, 0, 0, 0])
        if opt == a:
            cell[0] += int(n); cell[1] += int(k)
        elif opt == b:
            cell[2] += int(n); cell[3] += int(k)
    c = stratified_compare([tuple(v) for v in strata.values()])
    bad = c.p_worse >= 1 - ctx.s.guardrail_alpha and c.lift < -ctx.meta.objective.guardrail_max_drop
    return not bad, {"guardrail": "pickup_rate", **c.as_dict()}


def plan_routine(ctx: Ctx) -> list[Change]:
    if not enabled(ctx):
        return []
    s = ctx.s
    states = {d: arm_state(ctx, d) for d in EXPERIMENT_DIMS}
    active = sum(len(st["challengers"]) for st in states.values())
    changes = []
    for dim in EXPERIMENT_DIMS:
        if ctx.is_frozen(dim):
            continue
        st = states[dim]
        # Two horizons: promotion looks at the last `promote_window_days` (winners must be winning now);
        # adaptation (drop / raise) uses up to `adapt_window_days` so a 6% arm accumulates enough calls.
        since = ctx.now - s.promote_window_days * DAY
        table = ArmTable.build(ctx.data.arm_rows(ctx.wf, dim, since, ctx.now))
        ev = evaluate(table, st["champion"], st["challengers"], _code(ctx, dim), s.promote_alpha, s.promote_min_n)
        adapt_table = ArmTable.build(ctx.data.arm_rows(ctx.wf, dim, ctx.now - s.adapt_window_days * DAY, ctx.now))
        adapt = evaluate(adapt_table, st["champion"], st["challengers"], _code(ctx, dim), s.promote_alpha, 10**9)
        target = {o: w for o, w in st["dist"].items() if o not in st["draining"]}
        notes, intents, kinds = [], [], []
        impact = 0.0
        if ev.winners:
            w = ev.winners[0]
            ok, gev = _guardrail_ok(ctx, dim, w, st["champion"], since)
            c = ev.comparisons[w]
            # Don't promote an arm that is already fading: the last 24h must not point the other way.
            recent = ArmTable.build(ctx.data.arm_rows(ctx.wf, dim, ctx.now - DAY, ctx.now)).compare(
                ctx.data.code(dim, w), ctx.data.code(dim, st["champion"]))
            if recent.n_a >= 50 and recent.p_better < s.promote_recent_p:
                ok = False
                notes.append(f"{w} won over {s.promote_window_days} days but is not clearly ahead in the last 24h "
                             f"(lift {recent.lift:+.1%}); waiting")
            elif ok:
                target = {w: 1 - s.holdout_share, st["champion"]: s.holdout_share}
                lo, _ = c.lift_ci()
                impact = max(lo, 0.0) * (1 - s.holdout_share) / 0.10
                kinds.append("promote")
                notes.append(f"promote {w} over {st['champion']} (engagement {c.lift:+.1%}, n={c.n_a}, "
                             f"p={ev.pvalues[w]:.3g} Holm-adjusted family of {len(ev.pvalues)})")
                intents += [{"type": "meta", "key": f"promoted:{ctx.wf}:{dim}",
                             "value": {"winner": w, "over": st["champion"], "lift": c.lift,
                                       "lift_se": c.se / c.rate_b if c.rate_b else None, "minute": ctx.now}},
                            {"type": "arm", "dim": dim, "segment": "*", "option": w, "status": None},
                            {"type": "arm", "dim": dim, "segment": "*", "option": st["champion"],
                             "status": "holdout", "note": f"holdout after {w} promoted"}]
                for o in st["challengers"]:
                    if o != w:
                        intents.append({"type": "arm", "dim": dim, "segment": "*", "option": o,
                                        "status": "retired", "note": f"not better than {w}"})
                if st["holdout"] and st["holdout"] not in (w, st["champion"]):
                    intents.append({"type": "arm", "dim": dim, "segment": "*", "option": st["holdout"],
                                    "status": "retired", "note": "superseded holdout"})
            elif not ok and "not ahead" not in " ".join(notes):
                notes.append(f"{w} wins on engagement but fails the pickup guardrail; not promoted")
        if not kinds:
            recent_t = ArmTable.build(ctx.data.arm_rows(ctx.wf, dim, ctx.now - int(1.5 * DAY), ctx.now))
            for o in st["challengers"]:
                c = adapt.comparisons.get(o)
                if c is None:
                    continue
                r = recent_t.compare(ctx.data.code(dim, o), ctx.data.code(dim, st["champion"]))
                # The bar to remove an arm scales with how much traffic removal moves: a 6% arm goes at
                # P(worse) >= 0.8; a raised arm first steps back to 6% (below) and is only removed at 0.95.
                raised = target.get(o, 0) > s.explore_share + 1e-6
                bar = s.drop_prob_raised if raised else s.drop_prob
                fading = r.n_a >= s.fading_min_n and r.p_worse >= bar
                if (c.n_a >= s.drop_min_n and c.p_worse >= bar) or fading:
                    c = r if fading and not (c.n_a >= s.drop_min_n and c.p_worse >= bar) else c
                    target.pop(o, None)
                    active -= 1
                    kinds.append("drop_arm")
                    notes.append(f"drop {o} (P(worse than {st['champion']})={c.p_worse:.2f}, n={c.n_a})")
                    intents.append({"type": "arm", "dim": dim, "segment": "*", "option": o, "status": "dropped",
                                    "note": f"P(worse)={c.p_worse:.2f} n={c.n_a}"})
                    impact += 0.02
                elif raised and ((r.n_a >= s.raise_min_n and r.p_worse >= s.unraise_prob) or
                                 (c.n_a >= s.drop_min_n and c.p_worse >= s.drop_prob)):
                    # It earned extra traffic by being ahead; it no longer is. Give the traffic back - we
                    # don't need proof that it is worse to stop over-exposing customers to it.
                    target[o] = s.explore_share
                    kinds.append("drop_arm")
                    notes.append(f"scale {o} back to {s.explore_share:.0%} (no longer ahead: 36h lift {r.lift:+.1%} "
                                 f"n={r.n_a}; {s.adapt_window_days}d P(worse)={c.p_worse:.2f})")
                elif c.n_a >= s.raise_min_n and c.p_better >= s.raise_prob and target.get(o, 0) < s.raise_share - 1e-6 \
                        and (r.n_a < 50 or r.p_better >= s.raise_recent_p):
                    target[o] = s.raise_share
                    kinds.append("raise_arm")
                    notes.append(f"give {o} more traffic (P(better)={c.p_better:.2f}, n={c.n_a})")
                    impact += max(c.lift_ci()[0], 0) * s.raise_share / 0.10
            in_dim = sum(1 for o in target if o not in (st["champion"], st["holdout"]))
            slots = min(s.max_concurrent_challengers - active, s.max_challengers_per_dim - in_dim)
            for o in st["untested"][:max(slots, 0)]:
                target[o] = s.explore_share
                active += 1
                kinds.append("explore")
                notes.append(f"start testing {o} on {s.explore_share:.0%} of calls")
                impact += 0.01
        if not kinds:
            continue
        others = sum(w for o, w in target.items() if o != st["champion"])
        if "promote" not in kinds:
            target[st["champion"]] = max(1.0 - others, 0.5)
        target = quantize(target)
        if target == quantize(st["dist"]):
            continue

        def edit(cfg: dict, dim=dim, target=target) -> None:
            cfg["allocations"]["default"][dim] = target

        kind = next(k for k in ("promote", "raise_arm", "drop_arm", "explore") if k in kinds)
        changes.append(Change(
            kind=kind, summary=f"{dim}: " + "; ".join(notes),
            rationale=("Variants are randomised per call; comparisons are stratified by config version and cohort "
                       "so allocation changes cannot bias them. Winners need a Holm-corrected one-sided test "
                       f"(alpha={s.promote_alpha}) with >= {s.promote_min_n} connected calls per arm; the old "
                       f"champion keeps {s.holdout_share:.0%} as a holdout to catch decay."),
            edit=edit, evidence={"dim": dim, "state": st, "evaluation": ev.evidence()},
            dry_run=dry_run(ctx, dim, adapt_table, st["dist"], target),
            impact=impact, intents=intents, customer_facing=True,
            needs_approval=(kind in ("promote", "raise_arm"))))
    return changes


def plan_decay(ctx: Ctx) -> list[Change]:
    """Safety pass: a promoted winner that now probably loses to its holdout is rolled back."""
    s = ctx.s
    out = []
    for dim in EXPERIMENT_DIMS:
        st = arm_state(ctx, dim)
        if st["draining"]:
            out.append(_drain(ctx, dim, st))
            continue
        if not st["holdout"] or ctx.now % s.decay_check_every_minutes:
            continue
        champ, hold = st["champion"], st["holdout"]
        c = None
        for hours in (s.decay_window_hours, 2 * s.decay_window_hours):
            table = ArmTable.build(ctx.data.arm_rows(ctx.wf, dim, ctx.now - hours * 60, ctx.now))
            c = table.compare(ctx.data.code(dim, champ), ctx.data.code(dim, hold))
            if c.n_b >= s.decay_min_n:
                break
        if c is None or c.n_b < s.decay_min_n:
            continue
        promo = ctx.repo.meta(f"promoted:{ctx.wf}:{dim}") or {}
        promo_lift = promo.get("lift") if promo.get("winner") == champ else None
        promo_se = promo.get("lift_se") or 0.0
        se_rel = c.se / c.rate_b if c.rate_b > 0 else None
        # Fade test: recent lift vs the promoted lift, counting BOTH estimates' noise. The promoted lift is
        # also shrunk by one s.e. because it was selected for being significant (winner's curse).
        trend_z = None
        if promo_lift is not None and se_rel:
            trend_z = (c.lift - (promo_lift - promo_se)) / (se_rel ** 2 + promo_se ** 2) ** 0.5
        losing = c.p_worse >= s.decay_prob
        # "Stopped winning", not merely "wins by less": at large n a fade from +15% to +7% is significant
        # but rolling back then would hurt. Require the advantage to be essentially gone as well.
        faded = trend_z is not None and trend_z <= -s.decay_trend_z and c.lift <= s.decay_min_advantage
        if not (losing or faded):
            continue
        why = (f"probably worse than holdout {hold} (P={c.p_worse:.2f})" if losing else
               f"lift fell from {promo_lift:+.1%} at promotion to {c.lift:+.1%} (z={trend_z:.1f})")
        target = dict(st["dist"])
        target[hold] = target.get(hold, 0) + target.pop(champ, 0)
        target = quantize(target)

        def edit(cfg: dict, dim=dim, target=target) -> None:
            cfg["allocations"]["default"][dim] = target

        out.append(Change(
            kind="rollback", summary=f"{dim}: roll back {champ} -> {hold} (winner stopped winning: {why}, "
                                     f"last {hours}h)",
            rationale=(f"{champ} was promoted on evidence, but on recent calls it {why} (n={c.n_a}/{c.n_b}). "
                       "Effects can fade with exposure; we back out instead of waiting for certainty because the "
                       "downside is customer-facing. Options that lost to it are re-opened for testing."),
            edit=edit, evidence={"dim": dim, "comparison": c.as_dict(), "window_hours": hours,
                                 "promoted": promo, "trend_z": trend_z},
            dry_run={"method": f"winner vs holdout, last {hours}h, stratified", **c.as_dict()},
            impact=min(1.0, 0.3 + abs(c.lift)), customer_facing=True, safety=True,
            intents=[{"type": "arm", "dim": dim, "segment": "*", "option": champ, "status": "dropped",
                      "note": f"decayed: lift {c.lift:+.3f} vs {hold}"},
                     {"type": "arm", "dim": dim, "segment": "*", "option": hold, "status": None}] +
                    [{"type": "arm", "dim": dim, "segment": "*", "option": o, "status": None}
                     for o, row in ctx.repo.arm_status(ctx.wf, dim).items() if row["status"] == "retired"]))
    return out


def _drain(ctx: Ctx, dim: str, st: dict) -> Change:
    """Finish removing arms we already closed (a policy-limited auto step left some traffic on them)."""
    target = quantize({o: w for o, w in st["dist"].items() if o not in st["draining"]})

    def edit(cfg: dict, dim=dim, target=target) -> None:
        cfg["allocations"]["default"][dim] = target

    return Change(
        kind="rollback", summary=f"{dim}: finish moving traffic off {', '.join(st['draining'])} (closed earlier)",
        rationale="These options were dropped or rolled back by an earlier decision; the auto-apply step limit "
                  "left some traffic on them. Continue until they reach zero.",
        edit=edit, evidence={"dim": dim, "state": st}, dry_run={"method": "continuation of an applied decision"},
        impact=0.2, customer_facing=True, safety=True)


def dry_run(ctx: Ctx, dim: str, table: ArmTable, cur: dict, new: dict) -> dict:
    """Replay recent connected calls: expected engagement under current vs proposed allocation using each
    arm's observed rate. Arms without data are flagged rather than assumed."""
    rates, no_data = {}, []
    for o in set(cur) | set(new):
        code = ctx.data.code(dim, o)
        n, k = table.totals(code) if code is not None else (0, 0)
        if n >= 30:
            rates[o] = (k / n, n)
        else:
            no_data.append(o)
    ref = max(rates.items(), key=lambda kv: kv[1][1])[1][0] if rates else None

    def expect(d):
        if ref is None:
            return None
        return sum(w * rates.get(o, (ref, 0))[0] for o, w in d.items())

    e0, e1 = expect(cur), expect(new)
    return {"method": "replay of recent connected calls with observed per-arm engagement",
            "arm_rates": {o: {"engagement": round(r, 4), "n": n} for o, (r, n) in rates.items()},
            "no_evidence_arms": sorted(no_data),
            "expected_engagement_current": None if e0 is None else round(e0, 4),
            "expected_engagement_proposed": None if e1 is None else round(e1, 4)}
