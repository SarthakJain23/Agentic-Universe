"""Schedule component: cohort scheme + per-cohort hour weights (C3/C4).

First run: choose the cohort scheme from history and propose cohorts + hour weights together (cohorts
are never auto-applicable, so this always needs an approval). Afterwards weights are refreshed from
history + live data at most daily, and only when they moved materially; schedule-only changes are
auto-applied where the policy allows it.
"""
from __future__ import annotations

from ..analysis import schedule as S
from ..clock import MIN_PER_DAY
from ..domain.cohorts import CohortScheme
from .base import Change, Ctx


def scheme_of(ctx: Ctx) -> CohortScheme | None:
    data = ctx.repo.meta(f"scheme:{ctx.wf}")
    return None if data is None else CohortScheme.from_json(data)


def plan(ctx: Ctx) -> list[Change]:
    s, meta = ctx.s, ctx.meta
    hours = meta.hours
    applied = scheme_of(ctx)
    current = CohortScheme.from_config(ctx.config.get("cohorts") or [])
    if ctx.is_frozen("cohorts") or ctx.is_frozen("schedule"):
        return []
    if applied is not None and (current is None or
                                sorted(c.id for c in current.cells()) != sorted(c.id for c in applied.cells())):
        # Someone else (a supervisor) owns the cohort structure now. We do not overwrite it silently.
        return []
    if current is None:
        return []
    since = ctx.data.first_minute(ctx.wf) or -14 * MIN_PER_DAY
    rows = ctx.data.hour_feat_pickup(ctx.wf, since, ctx.now)
    if len(rows) == 0:
        return []
    last = ctx.repo.meta(f"schedule_at:{ctx.wf}")
    if applied is not None and last is not None and ctx.now - last < s.schedule_refresh_hours * 60:
        return []
    if applied is None:
        leads = ctx.data.lead_feats(ctx.wf, since, ctx.now)
        scheme, sel = S.choose_scheme(rows, leads, ctx.codec, hours, s)
        if not scheme.splits:
            ctx.repo.set_meta(f"schedule_at:{ctx.wf}", ctx.now)
            return []
    else:
        scheme, sel = applied, {"chosen": applied.features, "note": "refresh"}
    weights, cell_ev = S.hour_weights(rows, ctx.codec, scheme, hours, s)
    old = (ctx.config.get("schedule") or {}).get("hour_weights") or {}
    if applied is not None and S.weights_change(old, weights) < s.schedule_min_change:
        ctx.repo.set_meta(f"schedule_at:{ctx.wf}", ctx.now)
        return []
    lift = S.expected_pickup(rows, ctx.codec, scheme, hours, weights)
    cohorts = scheme.config_cohorts()
    new_scheme = applied is None

    def edit(cfg: dict) -> None:
        if new_scheme:
            cfg["cohorts"] = cohorts
        cfg.setdefault("schedule", {}).setdefault("lead_preferred_hour", {})
        cfg["schedule"]["hour_weights"] = weights

    primary_is_pickup = meta.objective.primary == "pickup_rate"
    impact = max(lift or 0.0, 0.0) / 0.10 * (1.0 if primary_is_pickup else 0.3)
    best = {c: e["best_hours"] for c, e in cell_ev.items()}
    summary = (f"Call each {'/'.join(scheme.features)} cohort in its best hours (est. pickup "
               f"{(lift or 0) * 100:+.1f}%)")
    return [Change(
        kind="cohorts+schedule" if new_scheme else "schedule", summary=summary,
        rationale=("Pickup by hour differs significantly between cohorts in our call history "
                   f"(feature selection: {sel.get('chosen')}). Weights only re-order who is dialled in each hour; "
                   "capacity, cadence and windows are untouched. No hour gets weight 0."),
        edit=edit, evidence={"feature_selection": sel, "cells": cell_ev},
        dry_run={"method": "replay of historical pickup by cohort x hour under the new weights "
                           "(approximate: ignores pool depletion)",
                 "expected_pickup_lift": None if lift is None else round(lift, 4), "best_hours": best},
        impact=impact, intents=[{"type": "meta", "key": f"scheme:{ctx.wf}", "value": scheme.to_json()},
                                {"type": "meta", "key": f"schedule_at:{ctx.wf}", "value": ctx.now}])]
