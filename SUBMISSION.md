# Outcome-First Agent Loop — Submission

**Name:** _(fill in)_ · **Date:** 2026-11-08 · **Hours actually spent (be honest):** _(fill in)_ · **Language / storage:** Python 3.11+ (httpx, numpy, stdlib) / one SQLite file (WAL)

> Engineer's guide (run, debug, extend, alert runbook): [docs/CONTROLLER.md](docs/CONTROLLER.md).
> Reports: [public_01](reports/public_01.md) · [public_02](reports/public_02.md) · [public_03 load](reports/public_03_load.md) · [crash recovery](reports/crash_recovery.log) · [audit example](reports/why_example.md).

---

## 0. TL;DR
- A single-process controller that drives the simulator. Each tick it ingests events into SQLite, reconciles governance, then per workflow runs incidents → safety → routine. Every change is one bundled, evidence-carrying proposal, routed to auto-apply or a human.
- Built well: crash-safe idempotent ingestion; one shared cohort object; schedule bootstrapped from history; caller-ID change-point detection and load rebalancing; stratified welcome/voice experiments with Holm-corrected promotion, holdouts and decay rollback; hallucination response that screens supervisor prompts with one validation run; a governance client that survives kill -9; a queryable audit store.
- **public_02:** conclusion rate **+17.0%** vs baseline (ks_cancel +51.2%). Prompt incident fixed in 75–210 min across runs (approver latency), flagged caller ID off within ~2h, **0 harmful changes**. **public_01:** **+1.8% to +4.1%** across runs (approver randomness), 0 harmful. **Load (12 workflows):** +5.3%, 0 harmful, 135k events/s, ~92 s wall per sim-day (budget 600 s).
- Cut: per-person call time, a web UI, cross-tenant fairness (C15), segment-specific allocations (designed, not wired in; §13), prompt *optimisation* (we only respond to prompt incidents).

## 1. Assumptions
1. `*_max_drop` in a guardrail is a **relative** drop (0.02 = 2% of the baseline rate). This is the stricter reading for rates around 0.25.
2. Welcome and voice **cannot move pickup**: they are heard after the call connects. Pickup-primary workflows (KYC) therefore get schedule and caller-ID work only, and no welcome/voice exploration that would only put the engagement guardrail at risk.
3. The grader's rule (a change is harmful if expected engagement drops >1% or caller-ID health drops >2% at apply time) is a good proxy for "harms a cohort". So the change that *starts* an experiment counts too, and exploration must be small.
4. A judgement-call rejection is a human "no". We don't re-ask the same kind of change for 4h, and we never resend the identical proposal: a re-proposal is rebuilt on fresh data with a new id. Product's "an expired proposal is a rejection" is implemented literally.
5. A supervisor edit is legitimate and final for 24h on the dims it touched. We may later propose reverting it, but only through approval, with evidence, and with the summary saying "reverts the supervisor change".
6. Reverting a *dangerous* supervisor prompt (one that fails the approver's own validation bar) via an approval request is not "silently overwriting".
7. Lead features arrive on `lead` events before that lead's calls; lead and person ids carry sequential numbers (there's a dictionary fallback otherwise).
8. Time-of-day pickup structure is stable across the run, so history (dialled under uniform weights) gives unbiased hour × cohort pickup rates.
9. Any variant's effect may fade with exposure. Every promotion therefore keeps a holdout.
10. A restarted simulator is a new world. The store archives itself rather than mixing worlds.
11. Validation pass rate measures safety only. Engagement evidence must come from production (§7).
12. Kestrel's supervisor's preferences (≤2 requests/day, none before 11:00) are honoured even though the policy allows 4/day from 10:00. They are configured per tenant in `settings.py`.
13. Approver identity is "the workflow's client approver", since the API exposes no named person. The audit records `approval_id` and the decision time.

## 2. Scope decisions
| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion | **Built** | Floor for everything; SRE non-negotiable | Partitioned tables + retention at 10× (§8) |
| C2 | Shared cohort object | **Built** | Product memo; prevents three definitions of "seniors" | Supervisor-defined cohorts imported as first-class Segments |
| C3 | Bootstrap from history | **Built** (schedule, flag baselines, known-good prompts) | Free evidence; no exploration cost | Use history for variant priors once history contains variants |
| C4 | Call-time optimisation | **Partial**: per cohort | Big, cheap, never harmful by construction | Per-person: Bayesian archetype posterior per returning person, gated by an A/B holdout |
| C5 | Caller-ID health | **Built** | public_02 incident; large pickup lever (load) | Learn the load→health curve explicitly |
| C6 | Welcome/voice experiments | **Built** (global allocation) | Primary-KPI lever for engagement workflows | Segment-specific allocations (heterogeneity test + extra cohort split); designed, not shipped (§13) |
| C7 | Promotion, decay, rollback | **Built** | Hidden scenarios grade decay rollback | Always-valid sequential tests to remove peeking inflation |
| C8 | Hallucination response | **Built** | public_02 incident; regulated clients | Shadow/synthetic engagement estimate before apply |
| C9 | Governance client | **Built** | Absolute constraint; crash safety lives here | Withdraw API (not offered) |
| C10 | Impact, affected leads, dry run | **Partial**: honest but approximate | Proposals are a product surface | Off-policy replay with per-lead propensities |
| C11 | Supervisor view | **Partial**: CLI/markdown | One screen, not fifty alerts | Read-only web page over the same store |
| C12 | Audit store | **Built** (`why`, `heard`) | Floor; compliance memo | — |
| C13 | Crash recovery demo | **Built** (`scripts/crash_test.sh`) | SRE non-negotiable | Run in CI on every commit |
| C14 | Loop observability | **Partial**: metrics table + `status` + alerts | On-call needs it | Export to Prometheus |
| C15 | Fair credit/attention allocation | **Cut** | Credits were never scarce (≤4 of 200 used); approver attention is per workflow by policy | Per-tenant budgets in the credit ledger |

**Principle.** First, whatever the constraints and the hidden-scenario grading depend on: governance, crash safety, incidents, decay and harm avoidance. Second, the levers with the best lift per unit of risk: schedule and caller ID cost no engagement risk. Last, anything that's mainly presentation. "Partial" means the honest core exists and is used by the loop.

## 3. Stakeholders

### 01 — Nikhil Rao, VP Sales
- **Accepted:** speed where it is safe. Schedule and caller-ID changes are auto-applied wherever the policy allows. A spam-flagged number is off within about 2h, and a bad prompt is rolled back as fast as the approver answers (75–210 min across runs; detection itself ≤30 min).
- **Pushed back:**
  - *"Live for everyone by evening."* A winner needs ≥400 connected calls per arm and a Holm-corrected test (about 2 days at our traffic), and it keeps a 10% holdout. In public_02 an early "winner" (W3) faded after 2 days; the loop pulled its traffic back automatically.
  - *"One blended score."* This violates hard constraint 3, and Product's definitions.
  - *"Don't look hesitant."* We batch: one request per workflow per slot.
- **Wrong against the policy:**
  - *"Kestrel turned on auto-apply for everything."* `ks_cancel` auto-apply covers schedule, caller_id, welcome and voice with `max_shift 0.2`; **not** prompt or cohorts.
  - *"Northwind is fine with auto-apply on KYC."* `nw_kyc` has `auto_apply.enabled: false`, and their Compliance head requires named human approval.
  - *"Persuasive prompt everywhere."* Prompt changes need validation evidence. P4's own summary ("freer objection handling") is the hallucination risk profile, and Product scopes winners to one workflow.
- **Reply:** "Happy to make 'improved itself overnight' true, and we can show it: in our incident test it rolled back a bad prompt within the approver's turnaround (75–150 minutes) and moved off a spam-flagged number in two hours, with the client's approver signing off. Two corrections before the summit. Kestrel's auto-apply covers timing, caller ID and small voice/greeting shifts, not scripts. Northwind KYC has auto-apply off. For the slide I'll give you per-client lift on each KPI separately; a blended score isn't something we can defend to a compliance head. Winners go live in days, not hours, because we've already seen 'lunchtime winners' that faded by day three."

### 02 — Meera Iyer, Head of Compliance (Northwind)
- **Accepted, all of it:**
  - Customer-facing changes (welcome, voice, prompt) on Northwind always go to the approver. `nw_renewal` auto-applies only schedule and caller_id, `nw_kyc` nothing.
  - Script changes carry `validation_run_id`.
  - Every call is joinable to the decision that made each variant live: `controller heard <person> --day N`, in seconds, not 24h.
  - Requests go out only when the approver can decide them the same day (`now + max latency ≤ approver end`), and are never sent if they would expire unread.
- **Pushed back:** nothing on substance. "Slow and right" is our default: new variants start on 6% of calls, so even in a pessimistic case a single change costs well under 2% engagement, and nothing is promoted without a holdout.
- **Wrong:** nothing. The hours (10–19, KYC 10–18) match the policy exactly.
- **Reply:** "Your conditions are how the system works. Anything a customer hears on Northwind goes to your approver; we never auto-apply it. Script changes carry a validation run id, and we additionally check engagement after go-live. For your auditors: for any customer and day we can show the opening line, voice and script they heard, which approved change made each one live, who approved it and when, in seconds. Requests reach your approvers only in hours they can decide them the same day. Operational changes (timing, caller ID) appear in the weekly summary with their reasons."

### 03 — Kabir Menon, Platform & SRE
- **Accepted:**
  - A kill/restart test exists (`scripts/crash_test.sh`, log in `reports/crash_recovery.log`): 4–5 random `kill -9`s, then invariants checked against the simulator API. No double apply, no lost approval, no duplicate events.
  - Idempotency everywhere (§9).
  - No new stateful infrastructure: one SQLite file plus a lock file.
  - Loop health without logs: `controller status`, the metrics table, and alerts split into page / ticket / info.
- **Pushed back:** the "Postgres and Redis" preference. I used SQLite instead of their Postgres. Same SQL, zero ops for this scale; the 10× plan moves to Postgres partitions (§8).
- **Wrong:** nothing.
- **Reply:** "Kill test: `scripts/crash_test.sh` kill -9s the controller at random points and checks the world afterwards (no proposal applied twice, every approval applied or closed with a reason, every chunk ingested once). Load: one simulated day of public_03 (840k events) costs ~92 s of controller wall time on a laptop, against your 600 s. State is one SQLite file next to the process; nobody operates it at 2 a.m. At 10× the first thing to break is the per-tick decision pass, and the fix is sharding workflows across processes onto your Postgres. Pages only for stale ingestion, stuck incidents or a second controller."

### 04 — Anita Desai, Ops Supervisor (Kestrel)
- **Accepted:**
  - Her prompt edits are never undone silently. A supervisor edit freezes the touched dims for 24h, and pending proposals go stale instead of being forced. When her P2 prompt failed validation, we *asked* her approver to revert it, with evidence, and the summary says "reverts the supervisor change".
  - At most 2 requests/day, none before 11:00.
  - One request carries all pending changes.
  - One screen (`controller view`).
  - Her "compliance script" fear: an LLM-suggested fix is only accepted after a post-apply engagement check against the pre-incident prompt (hour-matched). P5 ("near-verbatim compliance script") would fail it.
- **Pushed back:** *"Tell me before anything changes on a running campaign."* Kestrel's policy lets us auto-apply small (≤20%) voice/greeting exploration, timing and caller-ID changes. We do, and log each one in her view. Promotions and rollbacks of what customers hear still go to her approver.
- **Wrong:** her limits are stricter than her own policy (4/day, approvers from 10:00). We honour hers.
- **Reply:** "Your edits stand. If one of them trips a safety check, we'll ask you (with the evidence) rather than change it back ourselves. You'll get at most two requests a day, none before 11:00, and each bundles everything we want. Your one screen: `controller view --wf ks_cancel` shows what's being tested, what's winning, what's waiting for you and what changed today. Small test traffic (up to 20%) and caller-ID or timing tweaks go live without a request, as your policy allows, and are listed there. Any script fix has to keep people on the line, not just pass the safety test."

### 05 — Tara Kulkarni, Head of Product
- **Accepted:**
  - KPIs are never blended.
  - **One cohort object.** `Segment` has a canonical id like `age_band=60+`, and config cohorts are its disjoint cells; the same ids appear in evidence, the view and the audit.
  - Every proposal carries an impact score, diff, affected leads and dry run.
  - Expired is treated as rejected: re-queued on fresh data, never resent.
  - Only applied changes count. Learning state flips inside the transaction that marks a proposal APPLIED.
  - Winners stay per workflow.
- **Minimum cohort size and window (her open question):**
  - Cohort cells hold ≥4% of leads; segments are tested for heterogeneity only at ≥10%.
  - Promotion needs n ≥ 400 connected calls per arm over the last 2 days.
  - Cost: about 2 days to a decision at ~1,000 connected calls/day per workflow, and a measured **7.6% false-winner rate** per challenger over 8 days (A/A Monte Carlo of our exact rule).
  - Halving n to 200 roughly halves time-to-decision but doubles false winners.
- **Pushed back:** per-person call time as P0 (cut, §2). It has the most variance and the least evidence per lead, and a wrong per-person hour costs that person ×0.25 weight. Cohort-level call time, consolidated view and caller-ID health are delivered.
- **Wrong:** "impact score must mean something" is right, but *ranking* by it assumes scores are comparable across kinds. Ours are conservative (lower CI bound) for improvements, with fixed floors for protective changes (§6).
- **Reply:** "Definitions held: separate KPIs, one cohort object, proposals with impact, diff, affected leads and dry run, expired treated as rejected, and nothing counts as evidence unless it actually went live. My answer on cohort size and window: cells ≥4% of leads, ≥400 connected calls per arm over the last two days to call a winner. That's about two days per decision, with a measured false-winner rate around 7–8% (holdouts catch the rest). I cut per-person call time for this release; cohort-level timing captures most of the lift with far less risk. The supervisor view and caller-ID health are in."

### 06 — Aryamann, CTO
- **Accepted:**
  - *Explainability over size:* every change answers "why / who / what next" from our store.
  - *"Credits where they change a decision":* 0–4 credits per run. One 2-credit validation of a supervisor's prompt cuts detection from ~2.5h (production SPRT) to ≤30 min, leaving only the approver's latency. The LLM is called only when no known-good prompt exists.
  - *Design for the envelope:* §8.
  - *Choosing:* §2, and the replies above.
- **Pushed back:** nothing.
- **Wrong (in others' memos):** Sales on Kestrel and Northwind auto-apply; Anita's limits being stricter than her policy; Sales' "persuasive prompt" assumption (see 01, 04).
- **Reply:** "I chose governance, crash safety, incident response, decay handling and the two zero-risk levers (call timing, caller-ID health) over breadth. Per-person timing, a UI and cross-tenant fairness are cut, with reasons. Each change answers 'why, who approved, what happened next' from our own store. Credits: under 5 per run; the best spend was a 2-credit validation that cut detection of a bad supervisor prompt from hours to under 30 minutes. What breaks first at 10× is the per-tick decision pass, and it shards cleanly by workflow."

## 4. Architecture

```mermaid
flowchart LR
  SIM[(Simulator HTTP)] -- "POST /sim/advance (30m, 15m if incident)" --> LOOP
  subgraph Controller process
    LOOP[loop.py] --> ING[ingest/ chunk -> 1 txn]
    ING --> DB[(SQLite: calls, leads, sim_events,\nconfig_versions, proposals, arms,\nincidents, decisions, metrics, alerts)]
    LOOP --> GOV[governance/ reconcile]
    LOOP --> AG[agent.py per workflow]
    AG --> INC[planner/incident]
    AG --> SAFE[planner/callerid + experiments.decay]
    AG --> ROUT[planner/schedule + experiments + callerid.rebalance]
    INC & SAFE & ROUT --> AN[analysis/ + stats/ pure fns] --> DB
    INC & SAFE & ROUT --> RT[planner/router: 1 bundle -> auto | approval]
    RT --> GOV
    GOV -- "PUT config / POST approvals / POST apply" --> SIM
    GOV --> DB
  end
  DB --> CLI[why · heard · view · status]
```

Where state lives and what happens on restart is in [docs/CONTROLLER.md](docs/CONTROLLER.md#state-and-restart). In short: facts and checkpoints are committed together per chunk. A proposal row is written before its HTTP call and reconciled against the simulator on restart. Learning state changes only inside the APPLIED transaction. The simulator's clock is authoritative, and the loop resumes at its current minute.

## 5. The learning loop
- **Cohorts.**
  - One partition per workflow, chosen from history by a G-test. We compare "each feature value has its own hour profile" against "values differ only in level"; a split must beat its degrees of freedom by ≥30. On all public workflows this picks `age_band` (sometimes `× city_tier`), with nothing hardcoded.
  - Cells hold ≥4% of leads, ≤20 cells.
  - Statistics are computed on lead features joined from our store, stratified by (config version, cohort). Config cohorts therefore never fragment the evidence.
- **Experiment design.**
  - The simulator randomises welcome and voice independently per call, which makes a free 2-factor factorial; each dimension's marginal comparison is fair.
  - Exploration: ≤2 challengers per dimension and ≤3 in total, at 6% each, with the champion keeping the rest. If every new arm were 12% worse (a pessimistic case), a single change costs about −1.4% per dimension; with mixed effects the expectation is about 0.
  - Adaptation every 2h (one request per approval slot):
    - Drop a 6% arm at P(worse) ≥ 0.8 (n ≥ 150, or a 36h fade at n ≥ 80).
    - Raise to 25% at P(better) ≥ 0.9, n ≥ 150, with the last 36h agreeing (P ≥ 0.7).
    - A raised arm steps back to 6% at P(worse) ≥ 0.7 and is only fully dropped at ≥ 0.95. The bar scales with the traffic a decision moves.
  - Interactions: not modelled (§13).
- **Deciding a winner.**
  - One-sided stratified (Mantel–Haenszel) two-proportion test vs the concurrent champion over the last 2 days, with n ≥ 400 per arm. Holm across the challengers in that dimension, α = 0.05. The last 24h must still favour the winner (P ≥ 0.6). An arm-level pickup guardrail check also applies.
  - Deterministic: closed-form statistics, no sampling. The LLM never participates.
  - Measured false-winner rate: **7.6%** per challenger over 8 days. That's above nominal because we look every 2h; the fix is always-valid tests (§13).
- **Decay and rollback.**
  - The old champion keeps 10% as a holdout. Every 2h the winner is compared with it over 24h (48h if the holdout has fewer than 100 connected calls).
  - Roll back if P(winner < holdout) ≥ 0.95, *or* a fade test fires: the recent lift is below the promoted lift (shrunk by one s.e. for the winner's curse), z ≤ −2.5 counting both estimates' noise, *and* the advantage is gone (≤ +1%).
  - The rollback auto-applies its first policy-allowed step immediately and drains the rest; options that lost to the decayed winner are re-opened.
  - Observed speed: the decaying W3 was at 0% within about 1–2 days of onset in public_01/02. The fade test reacts fastest when the drop is large.
- **Call time and caller ID.**
  - *Call time:* per cohort, w(h) = (shrunk pickup(h) / the cohort's own mean)^2, floored at 0.1 (no wasted capacity). It's a comparative advantage, so it only re-orders who is dialled when. Fitted on history, refreshed daily.
  - *Caller ID, detection:* pairwise, hour-stratified pickup ratios between numbers. "Degraded" is a change-point: the ratio against *every* comparator with history fell below 0.6, by more than 30% vs the same pair's previous 48h, with p < 0.001. It's skipped if the number's own share surged, since that's load we caused. Exclusion is a 48h cooldown; afterwards the number is judged on fresh data at a 2% re-probe.
  - *Caller ID, steady state:* a damped rebalance (halfway to shares ∝ observed ratio). Load lowers health, so this converges toward balance. Unknown numbers get a 4% probe each.
- **Guardrail.** Promotion requires the winning arm not to be significantly worse on the guardrail KPI: per arm, randomised, so causal. Pickup-primary workflows get no welcome/voice exploration (assumption 2).

## 6. Governance
- **Lifecycle.** `SUBMITTING → PENDING → APPROVED → APPLYING → APPLIED`. Exits: `REJECTED`, `EXPIRED`, `LAPSED`, `STALE` (409 at apply), `FAILED` (4xx we caused); auto path `AUTO_SUBMITTING → APPLIED | FORBIDDEN | STALE`. Any non-applied exit leads to a *re-plan on fresh data* with a new proposal id. A 4h cool-down follows a human rejection.
- **Batching and timing.**
  - One bundle per workflow per routine slot, evaluated every 2h, and never while a request is pending.
  - Routine requests ≤ min(policy − 1, tenant cap); one slot per 24h is reserved for incidents and safety.
  - Sent only if `now + max latency ≤ approver end` (decided the same day) and ≥3h after the previous request.
  - Incident and safety requests skip pacing, but (a) wait for any pending request to resolve, so they don't go stale and burn a slot, and (b) are never sent if they would expire before the approver's next window (e.g. Kestrel's 12h TTL at 18:30).
  - Approved changes are applied on the next tick, ≤30 min against a 2h window.
- **Auto-apply.**
  - Used when the policy permits every changed dim and the shift fits.
  - Protective moves larger than `max_shift` go out as the largest allowed step, repeated on later ticks.
  - *Voluntarily not used:* promotions and "raise" steps on customer-facing dims always go to a human, even on Kestrel, where policy would allow small ones.
  - Cohort definitions are never auto-applicable (by policy anyway).
- **Impact, affected leads, dry run.**
  - *Impact:* the lower 95% bound of relative lift on the primary KPI, × affected share, ÷ 10%. Protective changes have fixed floors (incident 0.95, degraded number ≥ 0.4, rollback ≥ 0.3); exploration is about 0.01 per arm.
  - *Affected leads:* leads still inside their cadence and not concluded, in the touched cohorts.
  - *Dry run:* a replay of recent connected calls with each arm's observed rate (arms with no evidence are flagged, not assumed). Schedule dry runs reweight historical hour × cohort pickup; they're labelled approximate because they ignore pool depletion.
  - Honest, but not off-policy-exact (§13).
- **Supervisor edits mid-experiment.** In public_02 at D5 15:00, the supervisor set nw_renewal voice to V2 while our request was pending:
  - The edit is mirrored and audited (`supervisor_edit`).
  - Voice is frozen for 24h.
  - The pending request went STALE at apply (409) and was **not forced** (decision trail in §7).
  - Arms are re-derived from the live config, so the supervisor's V2 simply became the champion.

## 7. Incidents
- **Hallucination detection.**
  - (a) *Screening:* a non-controller prompt change triggers one validation run (n = 200, 2 credits) against the approver's own bar (0.97).
  - (b) *Window test:* a one-sided exact binomial on the last 6h vs the workflow's baseline flag rate from history. It needs p < 1e-5 and ≥2× baseline; with ~2,900 overlapping looks per load run, that's ~0.03 false incidents per run.
  - (c) *SPRT* on the flag rate since the prompt went live (capped at 48h), H1 = 2.5× baseline, α = 0.001.
- **Choosing a fix.**
  - First choice: the most recent prompt we've observed on this workflow with a normal flag rate (known-good; no credits needed).
  - Only if none exists: `/llm/suggest-fixes` (3 credits), taken in the order returned.
  - Each candidate must pass validation (n = 200; a second run at n = 500 if within 1pp of the bar).
- **Verification.** The flag-rate SPRT must accept the baseline. For LLM candidates only, engagement must not drop more than 6% vs the pre-incident prompt (hour-matched). Failing either sends us to the next candidate.
- **Credits.** 4 per public_02 run (two validations), 0 on public_01, 0 on load. Non-incident spend may not cross a 20-credit floor.
- **Caller ID.** See §5. Exclusion is a safety change: auto-applied in `max_shift` steps where allowed, an approval otherwise.

**public_02 timeline** (final run, from `decisions`):
```
D2 10:00  (hidden) ks_cancel D2 spam-flagged
D2 12:00  "Stop dialling from D2: pickup 56% of the other numbers"   auto-apply, step 1 of max_shift 0.2
D2 12:30  step 2 ... D2 reaches 0%                                   D2 share: 12.7% of day 2, 0% on D3-D4
D3 11:30  supervisor ships prompt P2 -> prompt dim frozen 24h, audit row
D3 12:00  prompt_screened: P2 validation pass rate 0.935 < 0.97 -> incident opened, page
D3 12:00  validation of P1 (known-good) 0.990 -> approval "Switch prompt P2 -> P1 (reverts the supervisor change to P2)"
D3 13:31  approver decides apr_00004      D3 14:00  applied            -> grader time to fix 150 min
D3 18:30  incident_resolved: P1 flags 5/398 back at baseline (engagement check skipped: known-good prompt)
D5+       D2 re-probed at ~1% after its 48h cooldown; still bad -> stays near zero
D5 15:00  supervisor sets nw_renewal voice=V2 -> voice frozen 24h; arms re-derived from the live config
```
Across runs, time to fix was 75–210 min. It is dominated by the approver's 20–120 min latency plus up to
one tick (15 min while urgent) before the decision surfaces. Detection itself takes ≤30 min because of the
screening validation.

## 8. Scale
Measured on `public_03_load` (12 workflows, 5 days, ~650k dials/day, ~840k events/day). Apple-silicon laptop, single process; the simulator runs on the same machine.

| metric | value |
|---|---|
| events ingested | 9.74M (6.38M history backlog + 840k/day live) |
| ingest throughput | **135k events/s** sustained (168k/s on the backlog); a live day ingests in ~9 s |
| controller wall per simulated day | **~92 s** (ingest 14 s, decide 70 s, simulator advance 7 s), against the 600 s budget |
| whole 5-day run | 460 s controller wall (486 s process incl. simulator start-up) |
| end-to-end decision lag | one tick of sim time (30 min; 15 during incidents) + ~1.5 s wall (decision step mean 1.46 s, p95 2.41 s); ingest lag 0 |
| peak memory | controller RSS 358 MB (~200 MB of it is SQLite page cache) |
| store size | 571 MB for 12 days of raw calls |
| outcome | ALL conclusion **+5.3%**, 49 changes, **0 harmful** ([report](reports/public_03_load.md)) |

- **What breaks first at 10×** (120 workflows, ~8.5M events/day):
  1. **The decision pass.** It scales linearly with workflows (≈6 s per workflow per sim-day, mostly round-trips: three config reads per workflow per tick, plus the detectors), reaching ~12 min/sim-day. Fix: shard workflows across N processes by `hash(tenant, workflow)`, each with its own store. Ingestion fans out by `workflow_id`, which every event carries. Also cache config and refresh on `config_changed`.
  2. **The `calls` table:** ~6.5M rows/day at 10×. Fix: partition by day (Postgres partitions or one SQLite file per day), roll windows older than 7 days into hour × cohort × arm aggregates, and keep raw rows only for the compliance horizon.
  3. **Ingestion is fine:** ~140–170k events/s single-threaded, so 8.5M/day ≈ 60 s/day.
  4. **Memory:** the lead index is a numpy array per workflow (8 bytes per lead), ~100 MB at 10×.
- **Partitioning:**
  - Workflows are independent except for credits (global) and approver attention (per workflow by policy).
  - Shard by tenant so a tenant's approver budget and fairness live in one place. Put the credit ledger in a small shared table with row locks.

## 9. Reliability
- **Crash test:** `scripts/crash_test.sh` ([log](reports/crash_recovery.log)).
  - Runs public_02 with 4–5 `kill -9`s at random wall-clock points (2–7 s apart, landing mid-ingest, mid-decision and mid-HTTP), then restarts with `./run.sh` and runs to completion.
  - `scripts/verify_run.py` then checks against the simulator API: no proposal_id applied twice; every APPLIED proposal live in config history at its version; every controller version known to our store; every approval applied or closed with a reason (stale/lapsed, never forced); every chunk ingested; no duplicate call events.
  - **All pass.** The killed-and-restarted run still scored +17.6% conclusion with 0 harmful changes; its prompt incident took 165 min to fix because restarts landed mid-incident.
  - **State lost:** none that matters. In-memory caches are rebuilt from the store, and the 2h routine pacing timer restarts.
  - The crash test also surfaced two real bugs, both fixed and covered by tests: duplicate incident fixes after a crash between "submit" and "incident updated", and a stray second controller colliding on proposal ids. Now there's a lock file, a per-store id nonce, and a page if a foreign controller appears.
- **Idempotency keys:**
  - `event_id` (`INSERT OR IGNORE`), plus a per-chunk checkpoint in the same transaction.
  - `proposal_id = <store-nonce>.<wf>.<minute>.<kind>.<n>`: the simulator's idempotency key; a duplicate returns 409 and triggers reconcile.
  - `approval_id`, looked up in config history after an apply.
  - Audit `dedup_key`.
- **Simulator errors:**
  - 5xx and connection errors are retried with exponential backoff on idempotent calls (GETs and keyed POSTs).
  - Credit-spending calls are never auto-retried.
  - `advance` after a timeout reads `/sim/state` before retrying, so we never advance twice.
  - Typed 4xx: 409 stale → STALE and re-plan; 409 duplicate → reconcile; 410 → LAPSED; 403 → FORBIDDEN (logged); 402 → incident notes "no credits"; 422 → FAILED plus a ticket.
  - A workflow whose calls fail this tick is skipped and retried next tick.

## 10. Audit & observability
**"Why did ks_cancel change at D3 14:00, who approved it, and what happened after?"**
```
$ python -m controller why ks_cancel --at "D3 14:00" --db state/public_02.db
ks_cancel changed at D3 14:00 (v8).
- actor: controller_approved · changed dims: prompt_version
- proposal: 3daa.ks_cancel.5040.incident.0 (approval, incident) created D3 12:00
- summary: Switch prompt P2 -> P1 (reverts the supervisor change to P2): P2 fails validation (pass rate 0.935 < 0.97)
- impact score: 0.95 · affected leads: 3219
- approved by: the ks_cancel client approver, approval apr_00004, decided D3 13:31, applied D3 14:00
## Rationale  [prompt_fix] supervisor prompt P2 fails validation ... P1 ... validation pass rate 0.990 on n=200 ...
## Diff / Evidence at decision time / Dry run   (full JSON in the output)
## What happened next
- 24h before: {'dials': 4800, 'pickup': 0.2383, 'engagement': 0.5, 'flag_rate': 0.0236}
- after (until D4 14:00): {'dials': 4800, 'pickup': 0.251, 'engagement': 0.517, 'flag_rate': 0.0108}
- D3 18:30 incident_resolved: P1 verified: flags 5/398 back at baseline ...
- D4 15:00 rejected: 3daa.ks_cancel.6600.routine.0 rejected: approver declined (judgement call)
```
Full output: [reports/why_example.md](reports/why_example.md). `controller heard <person> --day N` gives the compliance view: each call's variant, and the version and approval that made each dimension live.

**Page:** ingestion more than 2 sim-hours behind; an incident open for more than 6h; a foreign controller changing a workflow. **Ticket:** a proposal refused by the API (our bug); credits below the floor. **Info, no page:** supervisor edits, rejections, expiries, stale proposals, routine promotions and drops. Those are normal governance and live in the supervisor view.

## 11. Results
**public_02** ([report](reports/public_02.md)):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.356 | +51.2% | 0.245 | +35.2% | 0.497 | +6.3% | 32% | 71 (348) |
| nw_kyc | 0.379 | +1.3% | 0.244 | +4.0% | 0.596 | -2.0% | 0% | 31 (29) |
| nw_renewal | 0.434 | +0.2% | 0.266 | +1.2% | 0.551 | -0.7% | 7% | 54 (54) |
| ALL | 0.387 | +17.0% | 0.252 | +14.7% | 0.535 | +0.5% | 21% | 156 (431) |

24 changes applied: 8 beneficial, 16 neutral, **0 harmful**. 13 requests: 11 approved, 2 judgement-call rejections, 0 expired, 0 lapsed, 0 apply conflicts. Prompt incident time to fix 150 min (75–210 across runs). Flagged caller ID at 12.7% of day-2 dials (flagged at 10:00), 0% on days 3–4, ~1% after its re-probe. W3 (decaying) went 25% → 9% → 0% over days 5–7. Guardrails: nw_kyc engagement −2.0% (its guardrail allows 2%; its primary, pickup, +4.0%); nw_renewal pickup +1.2%. Earlier runs of the same code: +15.4% to +18.2%.

**public_01** ([report](reports/public_01.md)):

| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | engagement gap captured | hallucinated calls (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.350 | +2.5% | 0.245 | +0.1% | 0.503 | +2.3% | 3% | 63 (57) |
| nw_kyc | 0.376 | +2.7% | 0.247 | +2.2% | 0.605 | +1.3% | 0% | 31 (21) |
| nw_renewal | 0.425 | +2.0% | 0.263 | -0.6% | 0.554 | +2.4% | 6% | 39 (54) |
| ALL | 0.380 | +2.3% | 0.251 | +0.2% | 0.541 | +2.2% | 3% | 133 (132) |

14 changes applied: 3 beneficial, 11 neutral, **0 harmful**. W3 decayed from day 3; its share went 6% → 24% (raised while it still looked good) → 14% → 2% by day 7. Across the last six runs of the final code, ALL conclusion ranged **+1.8% to +4.1%**, all with 0 harmful changes. The spread comes from the approver's seeded randomness on proposal ids (which judgement calls get rejected) and from the simulator's outcome draws; ±2% per workflow is inside the noise for ~5k leads.

**Interpretation.**
- **Where we helped:**
  - Caller ID is the big lever. Moving off the flagged number and spreading load gave ks_cancel +35% pickup.
  - Prompt rollback: hallucinated calls fell from 348 to 71.
  - Schedule: +2–6% pickup on KYC, the primary KPI.
  - Experiments found W3/W2/V4 winners, and pulled W3 back when it decayed (to ~0 within 2–4 days of onset; faster when its decline is steep).
- **Where we were harmful, during development:**
  - (a) Load-driven caller-ID false positives. Fixed with pairwise change-points and the share-surge skip.
  - (b) Rollbacks of winners that were still winning at load-scenario sample sizes. Fixed by counting the promotion estimate's noise and the winner's curse.
  - (c) A noise-driven drop of a raised good arm. Fixed by exposure-scaled bars.
  - Each is visible in the commit history.
- **Weak spots:**
  - nw_kyc on public_01 is flat (±2% is inside run-to-run noise for ~5.6k leads), and a rejected first request costs it a day.
  - "Engagement gap captured" is modest (2–39% per workflow): segment-specific effects (V3 for 60+, V2 for Tamil) are invisible to global allocation.
- **What I'd change next:** segment-level allocations, and always-valid sequential tests.

## 12. Trade-offs & rejected alternatives
| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Thompson sampling across all arms | Standard bandit, fast | Randomised allocation is fine, but it pushes traffic to unproven arms, and each push is a graded config change. Instead: capped exploration plus deterministic raise/drop/promote rules. |
| Postgres / Redis | SRE already runs them | Zero-ops SQLite holds the load scenario with 6.5× headroom; Postgres is the 10× path (§8). |
| Per-call aggregates instead of raw calls | Smaller store | Raw calls answer the compliance "what did this customer hear" query and allow any new stratification. Aggregates are the 10× retention plan. |
| Ratio vs "all other numbers" for caller ID | Simple | Unstable when the pool changes (it caused false exclusions). Pairwise, hour-stratified change-points instead. |
| Auto-apply everything the policy allows (Kestrel) | Faster | Customer-facing promotions are what the supervisor asked to see. Hybrid instead. |
| Call the LLM on every incident | Matches the C8 checklist literally | Doesn't change the decision when a known-good prompt exists (costs 3 credits). LLM only as the fallback. |
| Wait for production evidence before reacting to a supervisor prompt | Zero credits | ~250 connected calls ≈ 2.5h of a bad prompt live. One 2-credit validation answers in zero sim time. |
| Segment cohorts for every heterogeneity signal | Captures V3-for-seniors effects | At ~65 connected/day per exploration arm, segment tests have little power and multiply comparisons. Deferred (§13). |

## 13. Known weaknesses
1. **Peeking.** Promotion is checked every 2h, giving a 7.6% false-winner rate in A/A simulation (nominal 5%). The holdout and rollback contain the damage, but the real fix is mSPRT or alpha-spending.
2. **No segment-specific allocations.** Effects confined to a segment (V3 +22% for 60+, −12% for 18–30) average out and are missed. The design exists (Segment cells, Holm across segments × arms) but isn't wired in.
3. **Decay detection is slow at small traffic:** a 10% holdout means ~100 connected calls/day. A −7% fade can take a day or more to confirm. A larger holdout costs lift.
4. **Dry runs are approximate:** the schedule dry run ignores pool depletion, and the arm dry run uses observed marginal rates rather than off-policy estimates.
5. **Approver randomness dominates small workflows:** with 2 requests/day and one reserved, a single judgement-call rejection costs a full day (nw_kyc).
6. **Single process:** the decision pass is the scaling bottleneck (§8).
7. **The view is CLI/markdown, not a web page;** the weekly compliance summary is a query, not a scheduled email.
8. **Thresholds were sanity-checked on public scenarios.** Every value has a stated statistical or operational reason in `settings.py`, and none reads scenario truth; but they've only been exercised on three worlds.

## 14. If this went to production at Formi next month
1. **Shadow mode first (days 1–3).** Run against live event streams with governance writes disabled. Compare its proposals with what humans did; calibrate impact scores against realised lift.
2. **Real store and real ingestion (days 1–5).** Postgres with day partitions; consume the event bus by workflow partition; an alert on ingest lag.
3. **Always-valid sequential tests (days 3–6).** Replace the fixed-n promotion test, and re-measure the A/A false-winner rate on replayed production data.
4. **One friendly tenant (day 6).** Enable on Kestrel `ks_cancel`: schedule and caller-ID auto-apply only. Everything customer-facing goes through approvals, with the supervisor view sent to Anita daily.
5. **Caller-ID integration (days 6–8).** Connect to the telco's real spam-flag feeds as a prior for the change-point detector.
6. **Segment allocations behind a flag (days 8–10),** measured against global allocation on one workflow.
7. **Sharding (days 10–14).** By tenant, plus credit and approver budgets per tenant (C15). Then expand to Northwind with Meera's weekly summary automated.
