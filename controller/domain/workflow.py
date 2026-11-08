"""Workflow metadata as returned by GET /workflows - policy, objective, options. Read-only value objects."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AutoApplyPolicy:
    enabled: bool
    allowed_dims: tuple[str, ...]
    max_shift: float

    def permits(self, dims) -> bool:
        return self.enabled and all(d in self.allowed_dims for d in dims)


@dataclass(frozen=True)
class Governance:
    auto_apply: AutoApplyPolicy
    approver_hours: tuple[int, int]
    approver_latency_minutes: tuple[int, int]
    proposal_ttl_hours: float
    apply_window_hours: float
    max_requests_per_24h: int
    min_validation_pass_rate: float


@dataclass(frozen=True)
class Objective:
    primary: str            # pickup_rate | engagement_rate
    guardrail: str          # the other one
    guardrail_max_drop: float   # relative drop we tolerate (assumption A6 in SUBMISSION.md)


@dataclass(frozen=True)
class WorkflowMeta:
    id: str
    tenant: str
    dialling_window: tuple[int, int]
    dials_per_hour: int
    cadence: tuple[int, ...]
    batch_hour: int
    batch_size: int
    mid_flight: str
    options: dict
    prompt_catalog: dict
    objective: Objective
    governance: Governance

    @property
    def hours(self) -> list[int]:
        lo, hi = self.dialling_window
        return list(range(lo, hi))

    @property
    def campaign_days(self) -> int:
        return len(self.cadence)

    @classmethod
    def from_api(cls, w: dict) -> "WorkflowMeta":
        g = w["governance"]
        aa = g.get("auto_apply") or {}
        obj = w["objective"]
        primary = obj["primary"]
        gr = obj.get("guardrail") or {}
        gname, gdrop = "engagement_rate" if primary == "pickup_rate" else "pickup_rate", 0.0
        for k, v in gr.items():  # e.g. pickup_rate_max_drop: 0.02
            if k.endswith("_max_drop"):
                gname, gdrop = k[: -len("_max_drop")], float(v)
        return cls(
            id=w["workflow_id"], tenant=w["tenant_id"], dialling_window=tuple(w["dialling_window"]),
            dials_per_hour=int(w["dials_per_hour"]), cadence=tuple(w["cadence"]),
            batch_hour=int(w["daily_batch"]["at_hour"]), batch_size=int(w["daily_batch"]["size"]),
            mid_flight=w["mid_flight"], options={k: list(v) for k, v in w["options"].items()},
            prompt_catalog=dict(w.get("prompt_catalog") or {}),
            objective=Objective(primary, gname, gdrop),
            governance=Governance(
                auto_apply=AutoApplyPolicy(bool(aa.get("enabled")), tuple(aa.get("allowed_dims") or ()),
                                           float(aa.get("max_shift") or 0.0)),
                approver_hours=tuple(g["approver_hours"]),
                approver_latency_minutes=tuple(g["approver_latency_minutes"]),
                proposal_ttl_hours=float(g["proposal_ttl_hours"]),
                apply_window_hours=float(g["apply_window_hours"]),
                max_requests_per_24h=int(g["max_requests_per_24h"]),
                min_validation_pass_rate=float(g.get("min_validation_pass_rate", 0.97)),
            ),
        )
