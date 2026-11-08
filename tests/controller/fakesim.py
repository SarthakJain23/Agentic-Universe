"""In-memory stand-in for the simulator's governance + event endpoints, following docs/SIMULATOR.md.
Used to test crash/reconcile paths deterministically without running the real world."""
from __future__ import annotations

import copy
import json

from controller.domain.configs import changed_dims, normalise
from controller.simclient import DuplicateProposal, NotApproved, StaleBase, Unavailable


class FakeSim:
    def __init__(self, config: dict, wf: str = "wf1"):
        self.wf = wf
        self.versions = [{"version": 1, "actor": "business_default", "config": normalise(config),
                          "proposal_id": None, "approval_id": None, "changed_dims": [], "applied_at": None}]
        self.approvals_: dict[str, dict] = {}
        self.proposal_ids: set[str] = set()
        self.fail_next: str | None = None      # "before" / "after" : simulate a crash around a call
        self.chunks: dict[int, list[dict]] = {}
        self.chunk_meta: list[dict] = []
        self.apply_calls = 0

    # ---- config
    def config(self, wf):
        v = self.versions[-1]
        return {"workflow_id": wf, "version": v["version"], "config": copy.deepcopy(v["config"])}

    def config_history(self, wf):
        return [{k: v[k] for k in ("version", "actor", "proposal_id", "approval_id", "changed_dims", "applied_at")}
                for v in self.versions]

    def _apply(self, cfg, actor, pid, aid):
        old = self.versions[-1]["config"]
        self.versions.append({"version": len(self.versions) + 1, "actor": actor, "config": normalise(cfg),
                              "proposal_id": pid, "approval_id": aid, "changed_dims": changed_dims(old, cfg),
                              "applied_at": "2027-01-04T10:00:00+05:30"})
        return self.versions[-1]["version"]

    def supervisor_edit(self, cfg):
        self._apply(cfg, "supervisor", None, None)

    # ---- governance
    def _crash(self, when):
        if self.fail_next == when:
            self.fail_next = None
            raise Unavailable(503, "unavailable", f"simulated crash {when}")

    def request_approval(self, body):
        self._crash("before")
        if body["proposal_id"] in self.proposal_ids:
            raise DuplicateProposal(409, "duplicate_proposal_id", body["proposal_id"])
        if body["base_version"] != self.versions[-1]["version"]:
            raise StaleBase(409, "stale_base_version", {"current_version": self.versions[-1]["version"]})
        self.proposal_ids.add(body["proposal_id"])
        aid = f"apr_{len(self.approvals_) + 1:05d}"
        self.approvals_[aid] = {"approval_id": aid, "proposal_id": body["proposal_id"], "workflow_id": body["workflow_id"],
                                "status": "pending", "base_version": body["base_version"],
                                "config": body["proposed_config"], "created_at": "2027-01-04T10:00:00+05:30",
                                "expires_at": "2027-01-05T10:00:00+05:30", "changed_dims": []}
        self._crash("after")
        return {k: v for k, v in self.approvals_[aid].items() if k != "config"}

    def decide(self, aid, status, reasons=()):
        a = self.approvals_[aid]
        a["status"] = status
        a["decided_at"] = "2027-01-04T11:00:00+05:30"
        a["reasons"] = list(reasons)

    def approval(self, aid):
        return {k: v for k, v in self.approvals_[aid].items() if k != "config"}

    def approvals(self, workflow_id=None, status=None):
        return [self.approval(a) for a in self.approvals_ if
                (not workflow_id or self.approvals_[a]["workflow_id"] == workflow_id)]

    def apply(self, wf, aid):
        self._crash("before")
        a = self.approvals_[aid]
        if a["status"] != "approved":
            raise NotApproved(409, "not_approved", {"status": a["status"]})
        if a["base_version"] != self.versions[-1]["version"]:
            raise StaleBase(409, "stale_base_version", {"current_version": self.versions[-1]["version"]})
        self.apply_calls += 1
        v = self._apply(a["config"], "controller_approved", a["proposal_id"], aid)
        a["status"] = "applied"
        self._crash("after")
        return {"workflow_id": wf, "version": v}

    def auto_apply(self, wf, body):
        self._crash("before")
        if body["proposal_id"] in self.proposal_ids:
            raise DuplicateProposal(409, "duplicate_proposal_id", body["proposal_id"])
        if body["base_version"] != self.versions[-1]["version"]:
            raise StaleBase(409, "stale_base_version", {"current_version": self.versions[-1]["version"]})
        self.proposal_ids.add(body["proposal_id"])
        v = self._apply(body["proposed_config"], "controller_auto", body["proposal_id"], None)
        self._crash("after")
        return {"workflow_id": wf, "version": v}

    # ---- events
    def add_chunk(self, events: list[dict]):
        n = len(self.chunk_meta)
        self.chunks[n] = events
        self.chunk_meta.append({"chunk": n, "kind": "events", "events": len(events),
                                "last_event_id": events[-1]["event_id"], "until_minute": 0})

    def list_chunks(self, kind, after=-1):
        return [m for m in self.chunk_meta if m["kind"] == kind and m["chunk"] > after]

    def iter_chunk_lines(self, n):
        for i, ev in enumerate(self.chunks[n]):
            if self.fail_next == f"chunk{n}" and i == len(self.chunks[n]) // 2:
                self.fail_next = None
                raise Unavailable(503, "unavailable", "connection dropped mid-chunk")
            yield json.dumps(ev).encode()
