"""The one shared cohort object (C2).

A `Segment` is a where-clause over lead features (AND across features, OR within a feature's values),
exactly the simulator's cohort semantics. Its id is derived from the clause, so the same population has
the same id everywhere: in detection evidence, in experiment arms, in config cohorts, in the supervisor
view and in the audit store ("age_band=60+" is always "age_band=60+").

A `CohortScheme` turns a few split features into a *disjoint partition* of cells; those cells become the
config's `cohorts`. Because cells are disjoint, the simulator's first-match rule cannot surprise us, and
every rule (schedule by age, allocation by language ...) is just "which cells does this segment cover".
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np

from ..ingest.codec import FEATURES, Codec


@dataclass(frozen=True)
class Segment:
    where: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def of(cls, where: dict | None) -> "Segment":
        return cls(tuple(sorted((f, tuple(sorted(map(str, vals)))) for f, vals in (where or {}).items())))

    @classmethod
    def parse(cls, sid: str) -> "Segment":
        if sid in ("*", "", "default"):
            return ALL
        where = {}
        for part in sid.split("&"):
            f, vals = part.split("=", 1)
            where[f] = vals.split(",")
        return cls.of(where)

    @property
    def id(self) -> str:
        if not self.where:
            return "*"
        return "&".join(f"{f}={','.join(v)}" for f, v in self.where)

    def as_dict(self) -> dict:
        return {f: list(v) for f, v in self.where}

    def matches(self, features: dict) -> bool:
        return all(str(features.get(f)) in vals for f, vals in self.where)

    def covers(self, other: "Segment") -> bool:
        """True if every lead in `other` is in self (other is at least as specific on self's features)."""
        o = dict(other.where)
        return all(f in o and set(o[f]) <= set(vals) for f, vals in self.where)

    def mask(self, feats: np.ndarray, codec: Codec) -> np.ndarray:
        """Boolean mask over an array of packed feature codes."""
        m = np.ones(len(feats), dtype=bool)
        for f, vals in self.where:
            i = FEATURES.index(f)
            codes = [codec.lookup(f"f:{f}", v) for v in vals]
            codes = [c for c in codes if c is not None]
            m &= np.isin((feats >> (8 * i)) & 255, codes)
        return m

    def __str__(self) -> str:
        return self.id


ALL = Segment()


@dataclass(frozen=True)
class CohortScheme:
    """Disjoint partition: the cartesian product of value-groups over up to a few split features."""
    splits: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = ()

    @classmethod
    def of(cls, splits: list[tuple[str, list[list[str]]]]) -> "CohortScheme":
        return cls(tuple((f, tuple(tuple(sorted(g)) for g in groups)) for f, groups in splits))

    @property
    def features(self) -> list[str]:
        return [f for f, _ in self.splits]

    def cells(self) -> list[Segment]:
        if not self.splits:
            return []
        out = []
        for combo in itertools.product(*[groups for _, groups in self.splits]):
            out.append(Segment.of({f: list(g) for (f, _), g in zip(self.splits, combo)}))
        return out

    def config_cohorts(self) -> list[dict]:
        return [{"id": c.id, "where": c.as_dict()} for c in self.cells()]

    def cells_in(self, seg: Segment) -> list[Segment]:
        """Cells entirely inside `seg` (seg must be expressible on the split features)."""
        return [c for c in self.cells() if seg.covers(c)]

    def to_json(self) -> list:
        return [[f, [list(g) for g in groups]] for f, groups in self.splits]

    @classmethod
    def from_json(cls, data) -> "CohortScheme":
        return cls.of([(f, groups) for f, groups in (data or [])])

    @classmethod
    def from_config(cls, cohorts: list[dict]) -> "CohortScheme | None":
        """Recover the scheme from a config's cohorts if (and only if) they form one of our partitions."""
        if not cohorts:
            return cls()
        segs = [Segment.of(c.get("where")) for c in cohorts]
        feats = sorted({f for s in segs for f, _ in s.where})
        groups = {f: sorted({v for s in segs for ff, v in s.where if ff == f}) for f in feats}
        scheme = cls.of([(f, [list(g) for g in groups[f]]) for f in feats])
        if sorted(c.id for c in scheme.cells()) == sorted(c["id"] for c in cohorts) and \
                all(c["id"] == Segment.of(c.get("where")).id for c in cohorts):
            return scheme
        return None
