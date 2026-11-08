# Load test — public_03_load

12 workflows (4 tenants), 5 simulated days after 7 days of history. About 650k dials/day and 840k
events/day live; 6.4M history events are ingested at start-up. Single controller process on an Apple-silicon
laptop (10 cores, of which the loop uses ~1); the simulator runs on the same machine.

Reproduce: `scripts/run_scenario.sh scenarios/public_03_load.yaml public_03 8723`, then
`python -m controller status --db state/public_03.db`.

## Throughput and latency (from the controller's own `metrics` table)

| sim day | events ingested | ingest s | decide s | simulator advance s | controller wall s | max ingest lag |
|---|---|---|---|---|---|---|
| D0 (incl. 6.4M history) | 6,377,573 | 37.9 | 67.5 | 5.9 | 111.3 | 0 |
| D1 | 840,038 | 8.2 | 66.1 | 7.2 | 81.5 | 0 |
| D2 | 840,028 | 8.5 | 69.8 | 7.4 | 85.7 | 0 |
| D3 | 840,022 | 8.7 | 73.1 | 7.2 | 89.0 | 0 |
| D4 | 840,019 | 8.9 | 74.1 | 7.4 | 90.4 | 0 |
| **total** | **9,737,680** | **72.1** | **350.6** | **35.1** | **460** (process: 486 s incl. simulator start-up) | **0** |

- **Ingest throughput:** 135k events/s sustained (168k/s on the history backlog). A live day ingests in ~9 s.
- **Wall per simulated day:** **~92 s**, against the SRE budget of 600 s, so 6.5× headroom. The decision pass
  is 76% of it.
- **Decision lag:** decisions are made in the same tick that ingests the events. Lag is one tick of sim time
  (30 min, or 15 during incidents) plus ~1.5 s of wall time (decision step mean 1.46 s, p95 2.41 s for 12
  workflows).
- **Memory:** controller peak RSS 358 MB (SQLite page cache ~200 MB plus a numpy lead index). The
  largest process in the run peaked at 375 MB.
- **Store:** 571 MB on disk for 12 days of raw calls (~4.3M call rows) plus everything else.
- **Credits:** 0 of 400 used (no incidents in this scenario).

## Outcomes (grader, vs no-op baseline)

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured |
|---|---|---|---|---|---|---|---|
| arav_cancel_2 | 0.347 | +2.9% | 0.248 | +1.8% | 0.500 | +0.7% | 2% |
| arav_kyc_1 | 0.360 | +0.2% | 0.217 | +1.0% | 0.594 | -1.1% | 0% |
| arav_renewal_0 | 0.443 | +9.3% | 0.267 | +1.3% | 0.586 | +7.0% | 40% |
| kest_cancel_2 | 0.362 | +7.5% | 0.246 | +1.4% | 0.523 | +4.8% | 35% |
| kest_kyc_1 | 0.355 | -0.1% | 0.214 | +0.0% | 0.595 | -0.3% | 0% |
| kest_renewal_0 | 0.433 | +7.7% | 0.268 | +1.4% | 0.573 | +5.2% | 26% |
| nort_cancel_2 | 0.367 | +8.1% | 0.248 | +1.8% | 0.523 | +4.9% | 38% |
| nort_kyc_1 | 0.358 | +0.6% | 0.217 | +0.7% | 0.592 | -0.6% | 0% |
| nort_renewal_0 | 0.441 | +8.9% | 0.268 | +1.3% | 0.585 | +6.8% | 38% |
| tide_cancel_2 | 0.364 | +6.0% | 0.248 | +1.4% | 0.521 | +4.0% | 33% |
| tide_kyc_1 | 0.362 | +1.7% | 0.217 | +1.2% | 0.599 | +0.2% | 0% |
| tide_renewal_0 | 0.443 | +9.4% | 0.268 | +1.6% | 0.585 | +7.1% | 41% |
| **ALL** | **0.386** | **+5.3%** | 0.244 | +1.3% | 0.564 | +3.3% | 22% |

49 changes applied: 17 beneficial, 32 neutral, **0 harmful**. Peak requests per workflow per 24h ≤ 2,
within every policy limit and Kestrel's own 2/day.

## Where it breaks at 10× (120 workflows, ~8.5M events/day)

1. **Decision pass, first.** It is linear in workflows: ≈6 s per workflow per sim-day, mostly HTTP round-trips
   (three config reads per workflow per tick) plus per-workflow SQL. At 120 workflows that is ~700 s per
   sim-day, over budget. Fix: shard workflows across processes (`hash(tenant)`), cache config and refresh it on
   `config_changed` events, and batch the detectors' SQL per tick.
2. **Store size.** ~6.5M call rows/day means ~45M/week. Fix: day partitions plus roll-ups of anything older than
   the analysis horizon (4 days) into hour × cohort × arm aggregates; raw rows kept only for the compliance
   horizon.
3. **Ingestion is not the problem.** At 135k events/s, 8.5M/day is ~63 s/day on one core.
