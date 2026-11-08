"""Crash/reconcile behaviour of the governance client, and idempotent ingestion."""
import pytest

from controller.analysis import did_health as H
from controller.domain.configs import normalise
from controller.governance.governor import Governor, Submission
from controller.ingest.codec import Codec, LeadIndex
from controller.ingest.ingestor import Ingestor
from controller.simclient import Unavailable
from controller.store import repo as R
from controller.store.db import connect
from controller.store.repo import Repo

from .fakesim import FakeSim

BASE = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                                   "caller_id": {"D1": 0.5, "D2": 0.5}}},
        "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}


@pytest.fixture
def env(tmp_path):
    conn = connect(tmp_path / "t.db")
    sim = FakeSim(BASE)
    repo = Repo(conn)
    gov = Governor(sim, repo, {})
    return sim, repo, gov


def _sub(sim, route="approval"):
    cfg = normalise(BASE)
    cfg["allocations"]["default"]["caller_id"] = {"D1": 0.75, "D2": 0.25}
    return Submission(wf="wf1", route=route, kind="routine", base_version=sim.config("wf1")["version"], config=cfg,
                      summary="move caller ids", diff={"caller_id": "x"}, affected_leads=10, dry_run={"x": 1},
                      impact_score=0.1, rationale="test", evidence={},
                      intents=[{"type": "meta", "key": "flag", "value": 1}])


def test_happy_path_applies_once_and_applies_intents(env):
    sim, repo, gov = env
    p = gov.submit(_sub(sim), 600)
    assert p.state == R.PENDING
    sim.decide(p.approval_id, "approved")
    gov.reconcile(630)
    assert repo.proposal(p.proposal_id).state == R.APPLIED
    assert repo.meta("flag") == 1
    gov.reconcile(660)
    assert sim.apply_calls == 1


def test_crash_after_request_reached_sim_is_reconciled_without_resend(env):
    sim, repo, gov = env
    sim.fail_next = "after"                      # sim accepted, our process "died" before recording it
    p = gov.submit(_sub(sim), 600)
    assert p.state == R.SUBMITTING
    gov.reconcile(630)
    p = repo.proposal(p.proposal_id)
    assert p.state == R.PENDING and p.approval_id == "apr_00001"
    assert len(sim.approvals_) == 1              # never sent twice


def test_crash_before_request_reached_sim_is_resent(env):
    sim, repo, gov = env
    sim.fail_next = "before"
    p = gov.submit(_sub(sim), 600)
    assert p.state == R.SUBMITTING and not sim.approvals_
    gov.reconcile(630)
    assert repo.proposal(p.proposal_id).state == R.PENDING and len(sim.approvals_) == 1


def test_crash_after_apply_does_not_double_apply(env):
    sim, repo, gov = env
    p = gov.submit(_sub(sim), 600)
    sim.decide(p.approval_id, "approved")
    sim.fail_next = "after"                      # apply succeeded in the world, we crashed before recording
    gov.reconcile(630)
    assert repo.proposal(p.proposal_id).state == R.APPLYING
    gov.reconcile(660)
    assert repo.proposal(p.proposal_id).state == R.APPLIED
    assert sim.apply_calls == 1 and len(sim.versions) == 2


def test_supervisor_edit_makes_approved_change_stale_not_forced(env):
    sim, repo, gov = env
    p = gov.submit(_sub(sim), 600)
    sim.decide(p.approval_id, "approved")
    edited = normalise(BASE)
    edited["allocations"]["default"]["voice"] = {"V2": 1.0}
    sim.supervisor_edit(edited)
    gov.reconcile(630)
    assert repo.proposal(p.proposal_id).state == R.STALE
    assert sim.versions[-1]["actor"] == "supervisor"     # their edit stands
    assert repo.meta("flag") is None                     # intents of a change that never went live are dropped


def test_auto_apply_crash_reconciled_via_config_history(env):
    sim, repo, gov = env
    sim.fail_next = "after"
    p = gov.submit(_sub(sim, route="auto"), 600)
    assert p.state == R.AUTO_SUBMITTING
    gov.reconcile(630)
    assert repo.proposal(p.proposal_id).state == R.APPLIED and len(sim.versions) == 2


def test_rejection_is_recorded_with_reasons(env):
    sim, repo, gov = env
    p = gov.submit(_sub(sim), 600)
    sim.decide(p.approval_id, "rejected", ["approver declined (judgement call)"])
    gov.reconcile(630)
    p = repo.proposal(p.proposal_id)
    assert p.state == R.REJECTED and p.reasons == ["approver declined (judgement call)"]


# ---------------------------------------------------------------- ingestion
def _events(start_id, n, wf="wf1"):
    evs = []
    for i in range(n):
        eid = start_id + i
        if i % 2 == 0:
            evs.append({"type": "lead", "ts": "2027-01-04T08:00:00+05:30", "workflow_id": wf, "campaign_id": f"{wf}:c0001",
                        "lead_id": f"{wf}:l{eid:08d}", "person_id": f"{wf}:p{eid:07d}",
                        "features": {"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "low"},
                        "event_id": eid})
        else:
            evs.append({"type": "call", "ts": "2027-01-04T10:03:00+05:30", "tenant_id": "t", "workflow_id": wf,
                        "campaign_id": f"{wf}:c0001", "lead_id": f"{wf}:l{eid - 1:08d}", "person_id": f"{wf}:p{eid - 1:07d}",
                        "attempt_no": 1, "campaign_day_index": 0, "config_version": 1, "cohort_id": "default",
                        "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
                        "connected": True, "engaged": i % 3 == 0, "disposition": "short_hangup", "duration_s": 3,
                        "turns": 1, "qa": {"hallucination_flag": False, "flow_adherence": 0.9}, "event_id": eid})
    return evs


def test_ingest_is_idempotent_and_survives_a_dropped_connection(tmp_path):
    conn = connect(tmp_path / "i.db")
    sim = FakeSim(BASE)
    sim.add_chunk(_events(1, 100))
    sim.add_chunk(_events(101, 100))
    ing = Ingestor(sim, conn, Codec(conn), LeadIndex(conn))
    sim.fail_next = "chunk1"
    with pytest.raises(Unavailable):
        ing.catch_up()
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 50      # chunk 0 committed, chunk 1 rolled back
    assert ing.last_chunk() == 0
    ing.catch_up()
    ing.catch_up()                                                                # no-op
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 100
    assert conn.execute("SELECT COUNT(*) FROM calls WHERE feat < 0").fetchone()[0] == 0   # every call joined to its lead


# ---------------------------------------------------------------- caller-ID health
def test_did_health_flags_the_bad_number_hour_by_hour():
    strata = []
    for b in range(10):
        strata += [(b, 0, 100, 30), (b, 1, 100, 12), (b, 2, 100, 31)]   # number 1 connects ~40% as often
    h = H.assess(strata, lambda c: f"D{c + 1}")
    assert h["D2"].ratio < 0.5 and h["D2"].p_low < 1e-6
    assert h["D1"].ratio > 1.0 and h["D1"].p_low > 0.5   # a flagged number in the pool makes the others look better
    target, why = H.target_distribution(h, ["D1", "D2", "D3", "D4"], {"D1": 0.4, "D2": 0.3, "D3": 0.3}, 0.7, 200, 0.04)
    assert "D2" not in target and target["D4"] == 0.04 and abs(sum(target.values()) - 1) < 1e-9
