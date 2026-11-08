"""Shared planner types: the per-workflow decision context and the `Change` unit.

Components (schedule, caller ID, experiments, incidents) each return `Change`s. A Change knows how to
edit a candidate config, why (summary/rationale/evidence), how big it is (impact, affected segment,
dry run), and what learning state flips if it goes live (`intents`). The bundler merges Changes into
one config edit per workflow and routes it (auto-apply vs approval).
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Callable

from ..analysis.data import Data
from ..domain.cohorts import ALL, Segment
from ..domain.workflow import WorkflowMeta
from ..ingest.codec import Codec
from ..settings import SETTINGS, Settings
from ..store.repo import Repo


@dataclass
class Ctx:
    now: int
    meta: WorkflowMeta
    version: int
    config: dict                     # current config exactly as the simulator returned it
    data: Data
    repo: Repo
    codec: Codec
    s: Settings = SETTINGS

    @property
    def wf(self) -> str:
        return self.meta.id

    def frozen_dims(self) -> dict[str, int]:
        return {d: until for d, until in (self.repo.meta(f"freeze:{self.wf}", {}) or {}).items() if until > self.now}

    def is_frozen(self, dim: str) -> bool:
        return dim in self.frozen_dims()


@dataclass
class Change:
    kind: str                         # schedule | cohorts | explore | drop_arm | raise_arm | promote | rollback |
                                      # did_rebalance | did_degraded | prompt_fix
    summary: str
    rationale: str
    edit: Callable[[dict], None]      # mutates a candidate config in place
    evidence: dict = field(default_factory=dict)
    dry_run: dict = field(default_factory=dict)
    impact: float = 0.0               # contribution to the [0,1] impact score (see planner/impact.py)
    affected: Segment = ALL
    intents: list = field(default_factory=list)
    customer_facing: bool = False     # welcome / voice / prompt
    needs_approval: bool = False      # by our own policy, even where auto-apply would be allowed
    safety: bool = False              # protective: may bypass routine pacing


def candidate(config: dict, changes: list[Change]) -> dict:
    out = copy.deepcopy(config)
    for ch in changes:
        ch.edit(out)
    return out
