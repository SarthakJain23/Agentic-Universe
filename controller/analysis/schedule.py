"""Call-time optimisation at cohort level (C3 bootstrap from history, C4).

1. Which features explain *when* people pick up? For each feature we compare a model where every value
   has its own hour profile against one where values differ only in level (deviance / G-test). The
   feature with the largest excess gain becomes the first split; a second split is kept only if it adds
   significant gain on top. Nothing is hard-coded to age.
2. For each resulting cell, an empirical-Bayes shrunk pickup rate per hour, divided by the cell's own
   average, is its *comparative advantage* in that hour. The schedule weight is that ratio^gamma,
   floored (never 0, so no capacity is wasted) and capped.

The dialler picks, each tick, the eligible leads with the highest weight x Gumbel noise, so weights only
re-order who is called when; capacity and cadence (business-owned) are untouched.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..domain.cohorts import CohortScheme, Segment
from ..ingest.codec import FEATURES, Codec
from ..settings import SETTINGS


@dataclass
class FeatureTable:
    """Decoded per-row feature values for a (feat, hour, dials, connected) aggregate."""
    values: dict[str, np.ndarray]   # feature -> array of value strings per row
    hour: np.ndarray
    n: np.ndarray
    k: np.ndarray


def decode(rows: np.ndarray, codec: Codec) -> FeatureTable:
    feats = rows[:, 0] if len(rows) else np.zeros(0, dtype=np.int64)
    vals = {}
    for i, f in enumerate(FEATURES):
        lut = codec.values(f"f:{f}")
        codes = (feats >> (8 * i)) & 255
        vals[f] = np.array([lut.get(int(c), "?") for c in codes], dtype=object)
    return FeatureTable(vals, rows[:, 1] if len(rows) else feats, rows[:, 2] if len(rows) else feats,
                        rows[:, 3] if len(rows) else feats)


def _cell_keys(t: FeatureTable, scheme: CohortScheme) -> np.ndarray:
    """Map every row to its cell id ('' if no cell)."""
    if not scheme.splits:
        return np.full(len(t.n), "*", dtype=object)
    keys = np.full(len(t.n), "", dtype=object)
    for cell in scheme.cells():
        m = np.ones(len(t.n), dtype=bool)
        for f, vals in cell.where:
            m &= np.isin(t.values[f], list(vals))
        keys[m] = cell.id
    return keys


def _grid(t: FeatureTable, keys: np.ndarray, hours: list[int]) -> tuple[list[str], np.ndarray, np.ndarray]:
    cells = sorted({k for k in keys if k})
    ci = {c: i for i, c in enumerate(cells)}
    hi = {h: i for i, h in enumerate(hours)}
    N = np.zeros((len(cells), len(hours)))
    K = np.zeros((len(cells), len(hours)))
    for key, h, n, k in zip(keys, t.hour, t.n, t.k):
        if key and int(h) in hi:
            N[ci[key], hi[int(h)]] += n
            K[ci[key], hi[int(h)]] += k
    return cells, N, K


def _deviance(N: np.ndarray, K: np.ndarray, P: np.ndarray) -> float:
    """G = 2 sum[k ln(k/E) + (n-k) ln((n-k)/(n-E))] with E = N*P (P = fitted rate under the null)."""
    E = np.clip(N * P, 1e-9, None)
    with np.errstate(divide="ignore", invalid="ignore"):
        a = np.where(K > 0, K * np.log(K / E), 0.0)
        b = np.where(N - K > 0, (N - K) * np.log((N - K) / np.clip(N - E, 1e-9, None)), 0.0)
    return float(2 * np.nansum(a + b))


def _level_shape_fit(N: np.ndarray, K: np.ndarray, shape: np.ndarray) -> np.ndarray:
    """Null model: p_ch = L_c * shape_ch (shape given per row). Fit L_c by matching totals."""
    exp = (N * shape).sum(axis=1, keepdims=True)
    L = np.where(exp > 0, K.sum(axis=1, keepdims=True) / np.clip(exp, 1e-9, None), 0)
    return np.clip(L * shape, 1e-6, 1 - 1e-6)


def feature_groups(lead_rows: np.ndarray, codec: Codec, feature: str, min_share: float) -> list[list[str]]:
    """Values with >= min_share of leads each get a group; the rest are pooled (and the pool is merged
    into the largest group if it is itself too small)."""
    i = FEATURES.index(feature)
    lut = codec.values(f"f:{feature}")
    counts: dict[str, int] = {}
    for feat, c in lead_rows:
        v = lut.get(int((feat >> (8 * i)) & 255))
        if v is not None:
            counts[v] = counts.get(v, 0) + int(c)
    total = sum(counts.values()) or 1
    big = sorted([v for v, c in counts.items() if c / total >= min_share])
    small = sorted([v for v, c in counts.items() if c / total < min_share])
    groups = [[v] for v in big]
    if small:
        if sum(counts[v] for v in small) / total >= min_share or not groups:
            groups.append(small)
        else:
            largest = max(groups, key=lambda g: sum(counts[v] for v in g))
            largest.extend(small)
    return groups


def choose_scheme(rows: np.ndarray, lead_rows: np.ndarray, codec: Codec, hours: list[int],
                  s=SETTINGS) -> tuple[CohortScheme, dict]:
    t = decode(rows, codec)
    if len(t.n) == 0:
        return CohortScheme(), {"reason": "no history"}
    # pooled hour shape
    hi = {h: i for i, h in enumerate(hours)}
    nh, kh = np.zeros(len(hours)), np.zeros(len(hours))
    for h, n, k in zip(t.hour, t.n, t.k):
        if int(h) in hi:
            nh[hi[int(h)]] += n
            kh[hi[int(h)]] += k
    pooled = np.where(nh > 0, kh / np.clip(nh, 1, None), 0)
    evidence: dict = {"gains": {}}
    chosen: list[tuple[str, list[list[str]]]] = []
    base_scheme = CohortScheme()
    base_shape_rows = None
    for depth in range(s.max_schedule_features):
        best = None
        for f in FEATURES:
            if f in [c[0] for c in chosen]:
                continue
            groups = feature_groups(lead_rows, codec, f, s.min_cell_share)
            if len(groups) < 2:
                continue
            scheme = CohortScheme.of(chosen + [(f, groups)])
            if len(scheme.cells()) > s.max_cohort_cells:
                continue
            cells, N, K = _grid(t, _cell_keys(t, scheme), hours)
            if depth == 0:
                shape = np.tile(pooled, (len(cells), 1))
                df = (len(cells) - 1) * (len(hours) - 1)
            else:
                # shape under the null = the parent cell's free hour profile
                pcells, PN, PK = base_shape_rows
                prate = np.where(PN > 0, PK / np.clip(PN, 1, None), 0)
                parent = {c: i for i, c in enumerate(pcells)}
                shape = np.zeros_like(N)
                for i, c in enumerate(cells):
                    seg = Segment.parse(c)
                    pid = Segment.of({ff: vv for ff, vv in seg.as_dict().items()
                                      if ff in [x[0] for x in chosen]}).id
                    shape[i] = prate[parent[pid]] if pid in parent else pooled
                df = (len(cells) - len(pcells)) * (len(hours) - 1)
            null = _level_shape_fit(N, K, shape)
            G = _deviance(N, K, null) - _deviance(N, K, np.clip(np.where(N > 0, K / np.clip(N, 1, None), 0.5),
                                                                   1e-6, 1 - 1e-6))
            excess = G - df
            evidence["gains"][f"{depth}:{f}"] = {"G": round(G, 1), "df": df, "excess": round(excess, 1),
                                                 "groups": groups}
            if excess >= s.schedule_feature_min_gain and (best is None or excess > best[0]):
                best = (excess, f, groups)
        if best is None:
            break
        chosen.append((best[1], best[2]))
        base_scheme = CohortScheme.of(chosen)
        cells, N, K = _grid(t, _cell_keys(t, base_scheme), hours)
        base_shape_rows = (cells, N, K)
    evidence["chosen"] = [f for f, _ in chosen]
    return base_scheme, evidence


def hour_weights(rows: np.ndarray, codec: Codec, scheme: CohortScheme, hours: list[int],
                 s=SETTINGS) -> tuple[dict[str, dict[str, float]], dict]:
    """Per-cell weights by hour. Returns ({cell_id: {"9": w, ...}}, evidence)."""
    t = decode(rows, codec)
    if not scheme.splits or len(t.n) == 0:
        return {}, {}
    cells, N, K = _grid(t, _cell_keys(t, scheme), hours)
    nh, kh = N.sum(axis=0), K.sum(axis=0)
    pooled = np.where(nh > 0, kh / np.clip(nh, 1, None), kh.sum() / max(nh.sum(), 1))
    prior = _level_shape_fit(N, K, np.tile(pooled, (len(cells), 1)))
    m = s.schedule_shrink_m
    rate = (K + m * prior) / (N + m)
    avg = (rate * np.clip(N, 1, None)).sum(axis=1, keepdims=True) / np.clip(N, 1, None).sum(axis=1, keepdims=True)
    ratio = rate / np.clip(avg, 1e-9, None)
    w = np.clip(ratio ** s.schedule_gamma, s.schedule_floor, s.schedule_cap)
    out = {c: {str(h): round(float(w[i, j]), 2) for j, h in enumerate(hours)} for i, c in enumerate(cells)}
    ev = {c: {"dials": int(N[i].sum()), "pickup": round(float(K[i].sum() / max(N[i].sum(), 1)), 4),
              "best_hours": [hours[j] for j in np.argsort(-ratio[i])[:3]]} for i, c in enumerate(cells)}
    return out, ev


def expected_pickup(rows: np.ndarray, codec: Codec, scheme: CohortScheme, hours: list[int],
                    weights: dict[str, dict[str, float]]) -> float | None:
    """Rough dry run: pickup if, in each hour, cells were dialled in proportion to (pool share x weight)
    instead of pool share. Ignores pool depletion and cadence - stated as approximate in proposals."""
    t = decode(rows, codec)
    if not scheme.splits or len(t.n) == 0:
        return None
    cells, N, K = _grid(t, _cell_keys(t, scheme), hours)
    rate = np.where(N > 0, K / np.clip(N, 1, None), 0)
    share = N.sum(axis=1) / max(N.sum(), 1)
    base = new = 0.0
    for j, h in enumerate(hours):
        wv = np.array([weights.get(c, {}).get(str(h), 1.0) for c in cells])
        pi0 = share / max(share.sum(), 1e-9)
        pi1 = share * wv / max((share * wv).sum(), 1e-9)
        base += float((pi0 * rate[:, j]).sum())
        new += float((pi1 * rate[:, j]).sum())
    return new / base - 1 if base > 0 else None


def weights_change(old: dict, new: dict) -> float:
    """Mean relative change across all cells x hours (1.0 if the cell sets differ)."""
    if set(old) != set(new):
        return 1.0
    diffs = []
    for c in new:
        for h, w in new[c].items():
            o = old[c].get(h, 1.0)
            diffs.append(abs(w - o) / max(o, 0.1))
    return float(np.mean(diffs)) if diffs else 0.0

