"""Hallucination detection (C8) on the automated QA flag.

The flag is imperfect (false positives and negatives), so the *baseline flag rate* is learned per
workflow from history rather than assumed zero. Two detectors, both population-level:

* sliding window: last N hours on the current prompt, one-sided exact binomial vs baseline, plus a
  minimum effect (rate >= 2x baseline) so a large sample can't make a trivial wobble "significant";
* SPRT since the current prompt went live: fast after a prompt change, with known error rates.

Expected false-positive rate: alpha=0.001 per test, ~20 window tests/day/workflow -> ~1 false incident
per 50 workflow-days before the 2x effect floor, which cuts it further (see SUBMISSION.md §7).
"""
from __future__ import annotations

from dataclasses import dataclass

from ..settings import SETTINGS
from ..stats import SPRT, binom_sf


@dataclass
class HallucinationCheck:
    triggered: bool
    reason: str
    evidence: dict


def baseline_rate(flags: int, connected: int, s=SETTINGS) -> float:
    if connected <= 0:
        return 0.015
    return max(flags / connected, s.halluc_baseline_floor)


def check(window: dict, since_change: dict, baseline: float, s=SETTINGS) -> HallucinationCheck:
    """window / since_change: {'connected': n, 'flags': k} for the current prompt."""
    n, k = window.get("connected", 0), window.get("flags", 0)
    ev = {"baseline_rate": round(baseline, 4), "window": {"connected": n, "flags": k,
                                                        "rate": round(k / n, 4) if n else None}}
    if n >= s.halluc_min_connected:
        p = binom_sf(k, n, baseline)
        ev["window"]["p_value"] = p
        if p < s.halluc_p and k / n >= s.halluc_min_ratio * baseline:
            return HallucinationCheck(True, f"flag rate {k / n:.1%} vs baseline {baseline:.1%} over {n} calls "
                                            f"(p={p:.1e})", ev)
    n2, k2 = since_change.get("connected", 0), since_change.get("flags", 0)
    sprt = SPRT(baseline, min(baseline * s.halluc_sprt_h1_ratio, 0.5), s.halluc_sprt_alpha, s.halluc_sprt_beta)
    verdict = sprt.decide(k2, n2) if n2 else "continue"
    ev["sprt"] = {"connected": n2, "flags": k2, "llr": round(sprt.llr(k2, n2), 2) if n2 else 0.0,
                  "upper": round(sprt.upper, 2), "verdict": verdict}
    if verdict == "h1" and n2 >= 30:
        return HallucinationCheck(True, f"SPRT: flag rate {k2 / n2:.1%} since prompt change vs baseline "
                                        f"{baseline:.1%} ({n2} calls)", ev)
    return HallucinationCheck(False, "within baseline", ev)


def healthy(stats: dict, baseline: float, s=SETTINGS) -> bool:
    """A prompt we have watched long enough to call known-good."""
    n, k = stats.get("connected", 0), stats.get("flags", 0)
    if n < s.known_good_min_connected:
        return False
    return binom_sf(k, n, baseline * 1.5) > 0.05 and k / n < s.halluc_min_ratio * baseline
