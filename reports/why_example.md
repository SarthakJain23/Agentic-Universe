# Why: ks_cancel @ D3 14:00

ks_cancel changed at D3 14:00 (v8).

- actor: **controller_approved**
- changed dims: prompt_version
- proposal: `3daa.ks_cancel.5040.incident.0` (approval, incident) created D3 12:00
- summary: Switch prompt P2 -> P1 (reverts the supervisor change to P2): P2 fails validation (pass rate 0.935 < 0.97)
- impact score: 0.95 · affected leads: 3219
- approved by: the ks_cancel client approver, approval `apr_00004`, decided D3 13:31, applied D3 14:00

## Rationale

[prompt_fix] supervisor prompt P2 fails validation: pass rate 0.935 on n=200 < approval bar 0.97. Candidate P1 chosen because it is a prompt that already ran on this workflow with a normal flag rate; validation pass rate 0.990 on n=200. After it goes live we verify flags return to baseline and engagement does not drop.

## Diff

```json
{
 "prompt_version": {
  "from": "P2",
  "to": "P1"
 }
}
```

## Evidence at decision time

```json
{
 "changes": [
  {
   "evidence": {
    "candidate": "P1",
    "detection": {
     "baseline_rate": 0.015242242787152967,
     "validation": {
      "credits_remaining": 198.0,
      "failures": 13,
      "n": 200,
      "pass_rate": 0.935,
      "prompt_version": "P2",
      "ts": "2027-01-07T12:00:00+05:30",
      "validation_run_id": "val_00001",
      "workflow_id": "ks_cancel"
     }
    },
    "source": "known_good",
    "validation_run_id": "val_00002"
   },
   "kind": "prompt_fix",
   "summary": "Switch prompt P2 -> P1 (reverts the supervisor change to P2): P2 fails validation (pass rate 0.935 < 0.97)"
  }
 ],
 "validation_run_id": "val_00002"
}
```

## Dry run

```json
{
 "prompt_fix": {
  "bad_prompt_since_live": {
   "connected": 57,
   "dials": 200,
   "engaged": 24,
   "first": 5017,
   "flags": 2,
   "last": 5033
  },
  "candidate_history": {
   "connected": 20222,
   "dials": 80409,
   "engaged": 10093,
   "first": -19617,
   "flags": 313,
   "last": 5000
  },
  "method": "production record of the candidate prompt on this workflow + validation run",
  "validation": {
   "credits_remaining": 196.0,
   "failures": 2,
   "n": 200,
   "pass_rate": 0.99,
   "prompt_version": "P1",
   "ts": "2027-01-07T12:00:00+05:30",
   "validation_run_id": "val_00002",
   "workflow_id": "ks_cancel"
  }
 }
}
```

## What happened next

- 24h before: {'dials': 4800, 'pickup': 0.2383, 'engagement': 0.5, 'flag_rate': 0.0236}
- after (until D4 14:00): {'dials': 4800, 'pickup': 0.251, 'engagement': 0.517, 'flag_rate': 0.0108}
- next change: v9 by controller_auto at D3 14:00 (caller_id)
- D3 18:30 incident_resolved: P1 verified: flags 5/398 back at baseline; known-good prompt: production engagement evidence exists; open-to-verified 390 min
- D4 02:00 applied: v10 live via auto-apply: Rebalance caller IDs to {'D1': 0.5869140625, 'D3': 0.4130859375}
- D4 15:00 rejected: 3daa.ks_cancel.6600.routine.0 rejected: approver declined (judgement call)
- D4 22:00 applied: v11 live via auto-apply: welcome_message: scale W2 back to 6% (no longer ahead: 36h lift -5.1% n=150; 4d P(worse)=0.29)
- D5 04:00 applied: v12 live via auto-apply: welcome_message: drop W2 (P(worse than W1)=0.85, n=118)
- D5 14:00 applied: v13 live via approval: voice: give V2 more traffic (P(better)=0.91, n=272)
- D5 14:00 applied: v14 live via auto-apply: Rebalance caller IDs to {'D1': 0.486328125, 'D2': 0.009765625, 'D3': 0.50390625}
