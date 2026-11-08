"""Pure functions over workflow configs, written from the SIMULATOR.md contract (§3, §4.2).

We re-implement `changed_dims` and `max_shift` ourselves (never importing sim/) so the planner can tell,
before calling the API, whether a change is auto-applicable and how far it moves traffic.
"""
from __future__ import annotations

import copy
import json

AGENT_DIMS = ("welcome_message", "voice", "caller_id")
EXPERIMENT_DIMS = ("welcome_message", "voice")
CHANGE_DIMS = ("cohorts", "welcome_message", "voice", "caller_id", "prompt_version", "schedule")


def normalise(cfg: dict) -> dict:
    """Canonical form: missing keys filled, weights normalised, zero weights removed, hours as strings."""
    out = {"cohorts": [{"id": c["id"], "where": {k: list(v) for k, v in (c.get("where") or {}).items()}}
                       for c in cfg.get("cohorts") or []],
           "allocations": {}, "prompt_version": cfg.get("prompt_version"),
           "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
    for key, dims in (cfg.get("allocations") or {}).items():
        out["allocations"][key] = {}
        for dim, weights in (dims or {}).items():
            out["allocations"][key][dim] = norm_dist(weights)
    sched = cfg.get("schedule") or {}
    for key, hw in (sched.get("hour_weights") or {}).items():
        out["schedule"]["hour_weights"][key] = {str(int(h)): float(w) for h, w in hw.items()}
    out["schedule"]["lead_preferred_hour"] = dict(sched.get("lead_preferred_hour") or {})
    return out


def norm_dist(weights: dict) -> dict:
    s = float(sum(w for w in weights.values() if w > 0))
    if s <= 0:
        return {}
    return {o: float(w) / s for o, w in weights.items() if w > 0}


def dist(cfg: dict, cohort: str, dim: str) -> dict:
    """Effective distribution for a cohort: its own, else default's (contract §3)."""
    return (cfg["allocations"].get(cohort) or {}).get(dim) or cfg["allocations"]["default"][dim]


def cohort_ids(cfg: dict) -> list[str]:
    return [c["id"] for c in cfg["cohorts"]]


def tv(a: dict, b: dict) -> float:
    return 0.5 * sum(abs(a.get(o, 0.0) - b.get(o, 0.0)) for o in set(a) | set(b))


def _close(a: dict, b: dict, eps: float = 1e-9) -> bool:
    return tv(a, b) <= eps


def changed_dims(old: dict, new: dict) -> list[str]:
    old, new = normalise(old), normalise(new)
    ch = set()
    if old["cohorts"] != new["cohorts"]:
        ch.add("cohorts")
    keys = set(old["allocations"]) | set(new["allocations"])
    for d in AGENT_DIMS:
        for k in keys:
            if not _close(_safe_dist(old, k, d), _safe_dist(new, k, d)):
                ch.add(d)
    if old["prompt_version"] != new["prompt_version"]:
        ch.add("prompt_version")
    if old["schedule"] != new["schedule"]:
        ch.add("schedule")
    return [d for d in CHANGE_DIMS if d in ch]


def _safe_dist(cfg: dict, cohort: str, dim: str) -> dict:
    try:
        return dist(cfg, cohort, dim)
    except KeyError:
        return {}


def max_shift(old: dict, new: dict) -> float:
    """Largest total-variation distance of any agent-dim distribution across cohorts (contract §4.2)."""
    old, new = normalise(old), normalise(new)
    keys = {"default"} | set(cohort_ids(old)) | set(cohort_ids(new))
    return max((tv(_safe_dist(old, k, d), _safe_dist(new, k, d)) for k in keys for d in AGENT_DIMS), default=0.0)


def step_toward(cur: dict, target: dict, max_tv: float) -> dict:
    """Move distribution `cur` toward `target` by at most `max_tv` total variation (TV is linear along the
    segment, so the interpolation factor is exact)."""
    d = tv(cur, target)
    if d <= max_tv + 1e-12:
        return dict(target)
    a = max(max_tv - 0.003, 0.0) / d   # headroom for quantisation (TV error <= options/4096)
    out = {o: (1 - a) * cur.get(o, 0.0) + a * target.get(o, 0.0) for o in set(cur) | set(target)}
    return quantize({o: w for o, w in out.items() if w > 1e-6})


def limit_shift(base: dict, target: dict, max_tv: float) -> dict:
    """A copy of `target` whose every agent-dim distribution is at most `max_tv` away from `base`.
    Cohorts/prompt/schedule are taken from `target` unchanged."""
    base, target = normalise(base), normalise(target)
    out = copy.deepcopy(target)
    # Only step distributions that are explicit in the target. A cohort that inherits `default` keeps
    # inheriting (it then moves exactly as far as default does); materialising its distribution here
    # would freeze it at this step forever.
    for k, dims in target["allocations"].items():
        for d in list(dims):
            if d not in AGENT_DIMS:
                continue
            b, t = _safe_dist(base, k, d), _safe_dist(target, k, d)
            if not b or not t or tv(b, t) <= max_tv + 1e-12:
                continue
            out["allocations"][k][d] = step_toward(b, t, max_tv)
    return out


QUANTUM = 1024


def quantize(d: dict) -> dict:
    """Weights as multiples of 1/1024 summing to exactly 1.0 (largest-remainder rounding).

    Binary fractions are exact in floating point, so the simulator's normalisation (w / sum) is the
    identity on them. Without this, re-sending an unchanged distribution could differ in the last bit,
    the simulator would report that dimension as changed, and an auto-apply that touches only an
    allowed dimension could be refused (or an approval would show a phantom diff)."""
    d = norm_dist(d)
    if not d:
        return {}
    raw = {o: w * QUANTUM for o, w in d.items()}
    units = {o: int(r) for o, r in raw.items()}
    left = QUANTUM - sum(units.values())
    for o in sorted(raw, key=lambda o: (-(raw[o] - units[o]), o))[:left]:
        units[o] += 1
    return {o: u / QUANTUM for o, u in sorted(units.items()) if u > 0}


def round_dist(d: dict, nd: int = 3) -> dict:
    """For display only."""
    return {o: round(w, nd) for o, w in norm_dist(d).items() if round(w, nd) > 0}


def fingerprint(cfg: dict) -> str:
    return json.dumps(normalise(cfg), sort_keys=True)


def diff(old: dict, new: dict) -> dict:
    """Human-readable, minimal diff for proposals and the audit trail."""
    old, new = normalise(old), normalise(new)
    out: dict = {}
    if old["cohorts"] != new["cohorts"]:
        out["cohorts"] = {"from": [c["id"] for c in old["cohorts"]], "to": new["cohorts"]}
    keys = sorted(set(old["allocations"]) | set(new["allocations"]) | set(cohort_ids(new)))
    for d in AGENT_DIMS:
        for k in keys:
            a, b = _safe_dist(old, k, d), _safe_dist(new, k, d)
            if not _close(a, b):
                out.setdefault(f"allocations.{d}", {})[k] = {"from": round_dist(a, 3), "to": round_dist(b, 3)}
    if old["prompt_version"] != new["prompt_version"]:
        out["prompt_version"] = {"from": old["prompt_version"], "to": new["prompt_version"]}
    if old["schedule"] != new["schedule"]:
        out["schedule.hour_weights"] = {k: v for k, v in new["schedule"]["hour_weights"].items()
                                        if old["schedule"]["hour_weights"].get(k) != v}
    return out
