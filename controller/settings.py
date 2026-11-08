"""Every tunable in one place, each with the reason it has the value it has.

Nothing here is derived from a scenario file. Values come from the README/SIMULATOR contract, from
statistics, or from stakeholder preferences we chose to honour (see SUBMISSION.md §3).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # ---- loop -----------------------------------------------------------------------------------
    # 30 sim-minutes per advance: the apply window is 2h, so we always apply an approval within
    # ~30 min of its decision, and a hallucination spike is tested every half hour.
    tick_minutes: int = 30

    # ---- ingestion ------------------------------------------------------------------------------
    insert_batch: int = 50_000          # rows per executemany inside one chunk transaction

    # ---- cohorts / schedule (C2-C4) ---------------------------------------------------------------
    max_cohort_cells: int = 20          # simulator hard limit
    min_cell_share: float = 0.04        # a cohort cell must hold >=4% of leads, else it is merged
    max_schedule_features: int = 2      # partition on at most two features (e.g. age x tier)
    schedule_feature_min_gain: float = 30.0   # deviance gain (chi2 ~ df*~10) needed to split on a feature
    schedule_gamma: float = 2.0         # weight = (relative pickup)^gamma; sharpens the hour preference
    schedule_floor: float = 0.1         # never 0: a zero hour silently wastes capacity
    schedule_cap: float = 6.0
    schedule_shrink_m: float = 60.0     # empirical-Bayes pseudo-dials toward the workflow hour profile
    schedule_min_change: float = 0.15   # mean |dw|/w below this -> not worth a config version
    schedule_refresh_hours: int = 24

    # ---- experiments (C6/C7) ----------------------------------------------------------------------
    explore_share: float = 0.06         # per challenger. 3 challengers x 6% x (worst plausible -12%)
    max_concurrent_challengers: int = 3  #   ~= -2% worst case; typical mixed effects ~ 0 (grader: -1%)
    max_challengers_per_dim: int = 2    # so one change never explores more than 12% of a dimension
    raise_share: float = 0.25           # a challenger that is probably better gets more traffic
    raise_prob: float = 0.90            # P(better) needed to raise ...
    raise_min_n: int = 150              # ... and at least this many connected calls (normal approx is poor below)
    unraise_prob: float = 0.70          # a raised arm whose last 36h say P(worse) >= 0.7 goes back to explore share
    raise_recent_p: float = 0.70        # a raise also needs the last 36h to agree (effects fade with exposure)
    drop_prob: float = 0.80             # P(worse) needed to drop an arm ...
    drop_min_n: int = 150               # ... once it has this many connected calls
    drop_prob_raised: float = 0.95      # removing a raised (25%) arm moves 4x the traffic: higher bar
    promote_min_n: int = 400            # connected calls per arm before a winner can be declared
    promote_window_days: int = 2        # winners must be winning *now*: effects fade with exposure
    adapt_window_days: int = 4          # drop/raise decisions use up to 4 days so small arms reach n
    fading_min_n: int = 80              # a challenger losing over the last 36h is dropped at this n
    promote_recent_p: float = 0.60      # and the last 24h must still favour them
    promote_alpha: float = 0.05         # family-wise, Holm across challengers x segments
    min_lift: float = 0.0               # a promoted arm must beat champion (one-sided)
    holdout_share: float = 0.10         # old champion keeps 10% after promotion, to detect decay
    decay_window_hours: int = 24
    decay_min_n: int = 100              # holdout connected calls needed in the decay window
    decay_prob: float = 0.95            # P(winner < holdout) that triggers rollback, or ...
    decay_trend_z: float = 2.5          # ... recent lift significantly below the lift we promoted on
    decay_check_every_minutes: int = 120  # sliding-window re-tests are correlated looks; fewer looks, fewer false rollbacks
    decay_min_advantage: float = 0.01   #     AND no longer meaningfully better (<= +1%) than the holdout
    segment_min_share: float = 0.10     # single-feature segments tested for heterogeneity
    max_segment_rules: int = 2          # segment-specific allocations per dimension
    experiment_on_pickup_primary: bool = False  # welcome/voice cannot move pickup; don't risk the guardrail

    # ---- caller ID (C5) ---------------------------------------------------------------------------
    did_window_hours: int = 6
    did_long_window_hours: int = 24
    did_min_dials: int = 200
    did_bad_ratio: float = 0.60         # observed/expected pickup vs the other numbers. Load alone moves a
                                        # number's ratio to ~0.8 (health = base/(1+(load/l0)^2)); flagged
                                        # numbers sit far lower, so the safety trigger is set well below that
    did_bad_p: float = 0.001
    did_sudden_drop: float = 0.70       # ratio now / ratio in the previous 48h: flagged = sudden, load = gradual
    did_cooldown_hours: int = 48        # an excluded number is re-measured after this (a false alarm is recoverable;
                                        # a truly flagged number fails again at low share and stays excluded)
    did_reprobe_share: float = 0.02     # a number back from cooldown is re-probed small: a still-flagged one costs <2%
    did_keep_ratio: float = 0.70        # below this a number gets no weight in the steady-state target
    did_probe_share: float = 0.04       # per never-used number; keeps a bad probe under the -2% bar
    did_rebalance_min_tv: float = 0.10

    # ---- hallucination incidents (C8) ---------------------------------------------------------------
    halluc_window_hours: int = 6
    halluc_min_connected: int = 100
    halluc_p: float = 1e-5              # one-sided binomial vs baseline flag rate. The 6h window is re-tested
                                        # every tick (~2,900 tests per load run): 1e-5 keeps false incidents ~0.03/run
    halluc_min_ratio: float = 2.0       # and observed rate >= 2x baseline
    halluc_sprt_h1_ratio: float = 2.5
    halluc_sprt_alpha: float = 0.001
    halluc_sprt_beta: float = 0.01
    halluc_baseline_floor: float = 0.005
    known_good_min_connected: int = 300
    validation_n: int = 200             # 2 credits
    validation_n_borderline: int = 500  # 5 credits, only if the first run is within 1pp of the bar
    fix_engagement_max_drop: float = 0.06  # post-fix engagement drop that sends us to the next candidate
    credit_floor: float = 20.0          # never spend below this on anything but an open incident

    # ---- governance (C9) ----------------------------------------------------------------------------
    incident_reserve_requests: int = 1  # request slots per 24h kept free for incidents
    max_routine_requests_per_day: int = 2
    min_request_spacing_minutes: int = 180
    routine_auto_spacing_minutes: int = 360
    supervisor_freeze_hours: int = 24   # dims a supervisor touched are left alone this long
    rejection_cooldown_minutes: int = 240  # a human said no: don't re-ask the same thing within 4h
    # Stakeholder preference (memo 04): Kestrel's supervisor is in standups until 11:00.
    tenant_quiet_until_hour: dict = field(default_factory=lambda: {"kestrel_finance": 11})
    tenant_max_routine_requests: dict = field(default_factory=lambda: {"kestrel_finance": 2})

    # ---- guardrail ----------------------------------------------------------------------------------
    guardrail_alpha: float = 0.05


SETTINGS = Settings()
