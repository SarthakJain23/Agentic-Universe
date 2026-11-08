# Controller: engineer's guide

For the engineer who owns this loop, gets paged by it, or extends it. The *why* behind each design choice
is in [SUBMISSION.md](../SUBMISSION.md); this page covers how to run it, read it, fix it and extend it.

## Run it

```bash
uv venv && uv pip install -r requirements.txt        # or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
python -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/mine01 &
./run.sh http://127.0.0.1:8700                       # runs to the end of the scenario; safe to kill and re-run

scripts/run_scenario.sh scenarios/public_02_incidents.yaml public_02 8722   # baseline + serve + run + grade
scripts/crash_test.sh                                 # kill -9 x4 at random points, restart, verify, grade
make test                                             # unit tests (≈1 s)
```

There are no services to start. All state lives in one SQLite file, `$CONTROLLER_DB` (default
`state/controller.db`), plus an `.lock` file next to it that stops two controllers from sharing the same
store.

## Ask it questions

| Question | Command |
|---|---|
| Why did workflow X change around T, who approved it, what happened next? | `python -m controller why nw_renewal --at "D3 14:00" --db state/x.db` |
| What did this customer hear on day N, and which decision made each variant live? | `python -m controller heard ks_cancel:p0018608 --day 3` |
| One screen per workflow: testing / winning / waiting for you / changed today | `python -m controller view [--wf ks_cancel] [--at "D5 12:00"]` |
| Is the loop healthy? (ingest rate, wall per sim-day, lag, credits, alerts) | `python -m controller status` |

Anything else is a SQL query against the store. Start with `decisions` (the audit trail), `proposals`
(every change attempted, with its full request) and `config_versions` (every version, including
supervisor edits).

## How a tick works ([agent.py](../controller/agent.py))

```
advance 30 min (15 while an incident / protective approval is open)
  ingest      ingest/ingestor.py   every new chunk, one transaction per chunk (facts + checkpoint)
  reconcile   governance/governor.py  every in-flight proposal vs GET /approvals, /config/history
  per workflow:
    sync      config mirror; a new supervisor version -> audit row + 24h freeze on the dims they touched
    incidents planner/incident.py  screen supervisor prompts, detect flag spikes, validate, propose, verify
    safety    planner/callerid.py (degraded number), planner/experiments.py (decay rollback / drain)
    routine   (every 2h, never while an approval is pending) schedule + experiments + caller-ID rebalance
              -> planner/router.py bundles into ONE change -> auto-apply or ONE approval request
  metrics row + loop health alerts
```

Layering rule: `analysis/` and `stats/` are pure functions with no I/O; only `analysis/data.py` runs SQL.
`planner/` turns analysis into `Change`s, `governance/` is the only code that changes a config, and
`agent.py` holds no business logic.

## State and restart

| State | Where | After a crash |
|---|---|---|
| Raw facts (calls, leads, other events) | `calls`, `leads`, `sim_events` | chunks after the `chunks` checkpoint are re-ingested; `INSERT OR IGNORE` on event_id |
| Proposals and their governance state | `proposals` | the row is written *before* the HTTP call; `reconcile()` resolves SUBMITTING / AUTO_SUBMITTING / APPLYING against the simulator, re-sending only if the world shows it never happened |
| Learning state (dropped / retired / holdout arms, cohort scheme, promotions) | `arms`, `meta` | changed only by `intents` inside the transaction that marks a proposal APPLIED |
| Incidents | `incidents` | deterministic ids (one per workflow × prompt × config version); the incident→proposal link is recovered from proposal intents |
| Audit trail | `decisions` | append-only; `dedup_key` makes re-running a tick idempotent |

A restarted *simulator* is a new world. The store detects this (its checkpoint is ahead of the
simulator's chunk count, or the fingerprint differs), archives itself, and starts fresh.

## Alerts and what to do

| Severity | Alert | Action |
|---|---|---|
| page | `ingestion is N sim-minutes behind` | Check `controller status` ingest seconds and the simulator's `/events/chunks`. Decisions are stale until it clears. |
| page | `hallucination flags up on Px` / `supervisor prompt Px fails validation` | A validated rollback is in the approval queue. Make sure a human sees it; `controller why <wf> --at <now>`. |
| page | `incident ... open for Nh` | The fix is stuck: rejected, expired, out of credits, or the request limit was hit. Read the incident's `incident_note` rows in `decisions`. |
| page | `config vN was applied by a controller that is not this one` | Two controllers are driving one workflow. Stop one now. |
| ticket | `proposal ... refused by API` | A 4xx we caused (invalid config). A bug: the proposal row has the full request. |
| ticket | `credits at N (< floor)` | Only incident work may spend credits now. Top up before the next incident. |
| info | `supervisor edit vN on [...]` | Nothing to do: those dims are frozen for 24h, and pending proposals will go stale instead of being forced. |

Not paged: rejections, expiries, stale proposals, routine drops and promotions. They are normal
governance outcomes and show up in the supervisor view.

## Extend it

- **New lever / detector.** Add `planner/<name>.py` with `plan(ctx) -> list[Change]`. Fill in `summary`,
  `rationale`, `evidence`, `dry_run`, `impact` and `intents`, and set `needs_approval` / `safety` honestly.
  Wire it into `_routine` or `_safety` in `agent.py`. Bundling, routing, budgets, audit and crash safety
  come for free.
- **New statistic.** Put it in `stats/` (pure, deterministic) and add a known-value test.
- **Tuning.** Every threshold lives in [settings.py](../controller/settings.py), each with its reason.
  Change it there, nowhere else.

## Tests

`tests/controller/` covers:
- config math, including quantisation stability under the simulator's normalisation and `limit_shift`;
- cohort partitions;
- stats, including a Simpson's-paradox case for the stratified comparison;
- the governor's crash paths against `fakesim.py` (crash before/after the request, crash after apply,
  supervisor edit leading to stale, auto-apply reconcile);
- ingest idempotency with a dropped connection mid-chunk;
- caller-ID health, including the pool-change case.

End-to-end behaviour is checked by `scripts/run_scenario.sh` (grader) and `scripts/crash_test.sh`
(invariants via `scripts/verify_run.py`).
