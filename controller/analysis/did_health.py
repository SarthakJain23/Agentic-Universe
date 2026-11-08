"""Caller-ID health (C5).

Health is not observable directly, but the simulator assigns a caller ID independently per dial, so in
any one hour all numbers dial a statistically identical population. For number d:

    expected_d = sum over hours h of dials_dh * (pickup rate of the *other* numbers in hour h)
    ratio_d    = connected_d / expected_d          (~ health_d / health_others)

Stratifying by hour removes time-of-day and allocation-change confounding. A number is *degraded* when
its ratio is low AND the shortfall is significant (one-sided z on the Poisson-binomial count).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

from ..stats import phi


@dataclass
class DidHealth:
    did: str
    dials: int
    connected: int
    expected: float
    ratio: float | None
    p_low: float          # P-value for "worse than the others"

    def as_dict(self) -> dict:
        return {"did": self.did, "dials": self.dials, "connected": self.connected,
                "expected": round(self.expected, 1), "ratio": None if self.ratio is None else round(self.ratio, 3),
                "p_low": round(self.p_low, 6)}


def assess(strata: list[tuple], name_of) -> dict[str, DidHealth]:
    """strata rows: (hour_bucket, did_code, dials, connected)."""
    by_b: dict[int, dict[int, tuple[int, int]]] = defaultdict(dict)
    for b, d, n, k in strata:
        by_b[int(b)][int(d)] = (int(n), int(k or 0))
    obs, exp, var, dials = defaultdict(int), defaultdict(float), defaultdict(float), defaultdict(int)
    for cells in by_b.values():
        N = sum(n for n, _ in cells.values())
        K = sum(k for _, k in cells.values())
        for d, (n, k) in cells.items():
            dials[d] += n
            obs[d] += k
            on, ok = N - n, K - k
            if on <= 0:
                continue
            p = ok / on
            exp[d] += n * p
            var[d] += n * p * (1 - p)
    out = {}
    for d in dials:
        e = exp[d]
        ratio = obs[d] / e if e > 0 else None
        z = (obs[d] - e) / math.sqrt(var[d]) if var[d] > 0 else 0.0
        out[name_of(d)] = DidHealth(name_of(d), dials[d], obs[d], e, ratio, phi(z) if var[d] > 0 else 1.0)
    return out


def target_distribution(health: dict[str, DidHealth], options: list[str], current: dict, keep_ratio: float,
                        min_dials: int, probe_share: float) -> tuple[dict, dict]:
    """Steady-state allocation: numbers with enough evidence of good health share traffic in proportion
    to ratio^2 (healthier numbers carry more, but load is still spread - health falls with load); numbers
    never tried get a small probe; numbers below `keep_ratio` get nothing."""
    known = {d: h for d, h in health.items() if d in options and h.dials >= min_dials and h.ratio is not None}
    good = {d: h.ratio ** 2 for d, h in known.items() if h.ratio >= keep_ratio}
    untried = [d for d in options if d not in health or health[d].dials < min_dials]
    # A number we are using but lack evidence on keeps its current share (no data is not bad data).
    pending = {d: current.get(d, 0.0) for d in untried if current.get(d, 0.0) > 0}
    probes = {d: probe_share for d in untried if d not in pending}
    reasons = {"good": sorted(good), "excluded": sorted(d for d in known if d not in good),
               "probing": sorted(probes), "insufficient_data": sorted(pending)}
    if not good and not pending:
        return dict(current), reasons | {"note": "no number with enough evidence of health; unchanged"}
    rest = 1.0 - sum(probes.values()) - sum(pending.values())
    tot = sum(good.values())
    out = {d: rest * w / tot for d, w in good.items()} if tot > 0 else {}
    if tot == 0 and pending:
        s = sum(pending.values())
        pending = {d: w / s * (1.0 - sum(probes.values())) for d, w in pending.items()}
    out.update(pending)
    out.update(probes)
    return out, reasons
