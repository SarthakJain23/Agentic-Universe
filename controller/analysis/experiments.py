"""Arm evaluation for welcome_message / voice experiments (C6, C7).

Unit: a connected call. Outcome: engaged. Every comparison is *stratified by (config version, cohort)*:
inside one stratum the allocation was fixed and the simulator randomised the variant independently per
dial, so arms in a stratum are exchangeable. Pooling across strata with Mantel-Haenszel weights avoids
Simpson's-paradox bias when allocations change while the population mix drifts over the day/week.

Welcome and voice are randomised independently, so each dimension's comparison is a fair marginal
(the other dimension is balanced in expectation) - a factorial design for free.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from ..domain.cohorts import Segment
from ..ingest.codec import Codec
from ..stats import Comparison, holm, stratified_compare


@dataclass
class ArmTable:
    """Connected-call counts: strata x option, optionally restricted to a segment."""
    n: dict[tuple, dict[int, int]]
    k: dict[tuple, dict[int, int]]

    @classmethod
    def build(cls, rows: np.ndarray, codec: Codec | None = None, segment: Segment | None = None) -> "ArmTable":
        """rows: cfg_version, cohort, feat, option, n, engaged."""
        if segment is not None and segment.where and len(rows):
            rows = rows[segment.mask(rows[:, 2], codec)]
        n: dict = defaultdict(lambda: defaultdict(int))
        k: dict = defaultdict(lambda: defaultdict(int))
        for ver, coh, _feat, opt, cnt, eng in rows:
            s = (int(ver), int(coh))
            n[s][int(opt)] += int(cnt)
            k[s][int(opt)] += int(eng)
        return cls(n, k)

    def totals(self, opt: int) -> tuple[int, int]:
        return (sum(v.get(opt, 0) for v in self.n.values()), sum(v.get(opt, 0) for v in self.k.values()))

    def compare(self, a: int, b: int) -> Comparison:
        strata = [(self.n[s].get(a, 0), self.k[s].get(a, 0), self.n[s].get(b, 0), self.k[s].get(b, 0))
                  for s in self.n]
        return stratified_compare(strata)


@dataclass
class DimEvaluation:
    champion: str
    comparisons: dict[str, Comparison]           # challenger -> vs champion
    winners: list[str]                            # Holm-significant, sorted by lift
    pvalues: dict[str, float]

    def evidence(self) -> dict:
        return {"champion": self.champion, "winners": self.winners,
                "arms": {o: c.as_dict() for o, c in self.comparisons.items()}}


def evaluate(table: ArmTable, champion: str, challengers: list[str], code_of, alpha: float,
             min_n: int, extra_tests: int = 0) -> DimEvaluation:
    """Compare each challenger with the champion; declare winners with Holm correction.

    `extra_tests` widens the Holm family when segment tests are run in the same decision, so the
    family-wise false-winner rate stays at `alpha` across everything we looked at."""
    cc = code_of(champion)
    comps, pvals = {}, {}
    for o in challengers:
        oc = code_of(o)
        if oc is None or cc is None:
            continue
        c = table.compare(oc, cc)
        comps[o] = c
        if c.n_a >= min_n and c.n_b >= min_n:
            pvals[o] = c.p_value_greater
    family = dict(pvals)
    for i in range(extra_tests):
        family[f"__seg{i}"] = 1.0
    rej = holm(family, alpha) if family else {}
    winners = sorted([o for o in pvals if rej.get(o) and comps[o].lift > 0], key=lambda o: -comps[o].lift)
    return DimEvaluation(champion, comps, winners, pvals)
