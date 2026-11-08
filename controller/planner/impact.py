"""Honest impact score and affected-lead counts (C10).

impact_score (what the supervisor's queue is ranked by) = clip(sum of per-change impacts, 0, 1), where a
change's impact is:
  * expected improvements: the *lower* 95% bound of the relative lift on the workflow's primary KPI,
    scaled so a 10% lift on everyone = 1.0, times the share of traffic it touches;
  * protective changes (incidents, degraded caller IDs, decay rollbacks): a fixed high floor (0.4-0.95)
    because their value is avoided harm, which a lift estimate does not capture;
  * exploration: ~0.01 per arm - learning value only, honestly small.
Nothing is inflated to get approved: an exploratory request should sit at the bottom of the queue.

affected_leads = leads still inside their cadence window and not concluded, in the cohorts touched.
"""
from __future__ import annotations

from ..domain.cohorts import ALL
from .base import Change, Ctx


def impact_score(changes: list[Change]) -> float:
    return round(min(max(sum(c.impact for c in changes), 0.0), 1.0), 3)


def affected_leads(ctx: Ctx, changes: list[Change]) -> int:
    rows = ctx.data.active_leads(ctx.wf, ctx.now, ctx.meta.campaign_days)
    if len(rows) == 0:
        return 0
    total = int(rows[:, 1].sum())
    if any(c.affected == ALL for c in changes):
        return total
    n = 0
    for c in changes:
        n += int(rows[c.affected.mask(rows[:, 0], ctx.codec), 1].sum())
    return min(n, total)
