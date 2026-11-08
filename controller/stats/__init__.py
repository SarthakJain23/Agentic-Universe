"""Small, dependency-free statistics. Everything is deterministic (closed forms, no sampling), which is
what constraint 4 asks of winner selection: same data in, same decision out."""
from __future__ import annotations

import math
from dataclasses import dataclass


def phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, c - h), min(1.0, c + h)


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exact (log-space)."""
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    lg = math.lgamma
    base = lg(n + 1)
    terms = [base - lg(i + 1) - lg(n - i + 1) + i * lp + (n - i) * lq for i in range(k, n + 1)]
    m = max(terms)
    return min(1.0, math.exp(m) * sum(math.exp(t - m) for t in terms))


@dataclass(frozen=True)
class Comparison:
    """Arm A vs reference B on a binary outcome, stratified (Mantel-Haenszel weights)."""
    n_a: int
    k_a: int
    n_b: int
    k_b: int
    diff: float          # stratified risk difference A - B
    se: float
    rate_b: float        # pooled reference rate

    @property
    def z(self) -> float:
        return self.diff / self.se if self.se > 0 else 0.0

    @property
    def p_better(self) -> float:
        """P(A > B) under a normal approximation to the posterior of the difference (flat prior)."""
        return phi(self.z) if self.se > 0 else 0.5

    @property
    def p_worse(self) -> float:
        return 1.0 - self.p_better if self.se > 0 else 0.5

    @property
    def p_value_greater(self) -> float:
        """One-sided p-value for H1: A > B."""
        return 1.0 - phi(self.z) if self.se > 0 else 1.0

    @property
    def lift(self) -> float:
        return self.diff / self.rate_b if self.rate_b > 0 else 0.0

    def lift_ci(self, z: float = 1.96) -> tuple[float, float]:
        if self.rate_b <= 0:
            return 0.0, 0.0
        return (self.diff - z * self.se) / self.rate_b, (self.diff + z * self.se) / self.rate_b

    def as_dict(self) -> dict:
        lo, hi = self.lift_ci()
        return {"n_a": self.n_a, "k_a": self.k_a, "n_b": self.n_b, "k_b": self.k_b,
                "rate_a": round(self.k_a / self.n_a, 4) if self.n_a else None,
                "rate_b": round(self.k_b / self.n_b, 4) if self.n_b else None,
                "lift": round(self.lift, 4), "lift_ci95": [round(lo, 4), round(hi, 4)],
                "p_better": round(self.p_better, 4), "p_one_sided": round(self.p_value_greater, 5)}


def stratified_compare(strata: list[tuple[int, int, int, int]]) -> Comparison:
    """strata: (n_a, k_a, n_b, k_b) per stratum (e.g. per config version x cohort, where the allocation was
    constant). Within a stratum assignment is random, so comparing inside strata removes Simpson's-paradox
    bias from allocations that changed over time while the population mix drifted."""
    num = wsum = var = 0.0
    na = ka = nb = kb = 0
    for n1, k1, n2, k2 in strata:
        na, ka, nb, kb = na + n1, ka + k1, nb + n2, kb + k2
        if n1 == 0 or n2 == 0:
            continue
        w = n1 * n2 / (n1 + n2)
        p1, p2 = k1 / n1, k2 / n2
        # Agresti-style smoothing for the variance only, so p=0/1 strata don't claim certainty
        q1, q2 = (k1 + 0.5) / (n1 + 1), (k2 + 0.5) / (n2 + 1)
        num += w * (p1 - p2)
        var += w * w * (q1 * (1 - q1) / n1 + q2 * (1 - q2) / n2)
        wsum += w
    if wsum == 0:
        return Comparison(na, ka, nb, kb, 0.0, 0.0, kb / nb if nb else 0.0)
    return Comparison(na, ka, nb, kb, num / wsum, math.sqrt(var) / wsum, kb / nb if nb else 0.0)


def holm(pvalues: dict, alpha: float) -> dict:
    """Holm-Bonferroni step-down. Returns {key: rejected?}. Controls family-wise error at alpha."""
    order = sorted(pvalues, key=lambda k: pvalues[k])
    m = len(order)
    out = {k: False for k in pvalues}
    for i, k in enumerate(order):
        if pvalues[k] <= alpha / (m - i):
            out[k] = True
        else:
            break
    return out


@dataclass(frozen=True)
class SPRT:
    """Wald's sequential probability ratio test for a Bernoulli rate: H0 p=p0 vs H1 p=p1 (p1 > p0)."""
    p0: float
    p1: float
    alpha: float
    beta: float

    def llr(self, k: int, n: int) -> float:
        return k * math.log(self.p1 / self.p0) + (n - k) * math.log((1 - self.p1) / (1 - self.p0))

    @property
    def upper(self) -> float:
        return math.log((1 - self.beta) / self.alpha)

    @property
    def lower(self) -> float:
        return math.log(self.beta / (1 - self.alpha))

    def decide(self, k: int, n: int) -> str:
        v = self.llr(k, n)
        if v >= self.upper:
            return "h1"
        if v <= self.lower:
            return "h0"
        return "continue"
