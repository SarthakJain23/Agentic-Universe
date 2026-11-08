import math

from controller.domain.cohorts import CohortScheme, Segment
from controller.domain.configs import changed_dims, limit_shift, max_shift, normalise, quantize, step_toward, tv
from controller.stats import SPRT, binom_sf, holm, stratified_compare, wilson

BASE = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1}, "voice": {"V1": 1},
                                                   "caller_id": {"D1": 0.5, "D2": 0.5}}},
        "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}


# ---------------------------------------------------------------- configs
def test_quantize_sums_to_exactly_one_and_is_stable_under_simulator_normalisation():
    for d in ({"a": 0.88, "b": 0.06, "c": 0.06}, {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}, {"x": 0.7011, "y": 0.2989}):
        q = quantize(d)
        assert sum(q.values()) == 1.0
        s = float(sum(q.values()))
        assert {o: w / s for o, w in q.items()} == q        # what the simulator does: identity


def test_changed_dims_and_shift():
    new = normalise(BASE)
    new["allocations"]["default"]["caller_id"] = {"D1": 0.25, "D2": 0.25, "D3": 0.5}
    assert changed_dims(BASE, new) == ["caller_id"]
    assert math.isclose(max_shift(BASE, new), 0.5)


def test_limit_shift_respects_max_and_does_not_materialise_inherited_cohorts():
    cfg = normalise(BASE)
    cfg["cohorts"] = [{"id": "age_band=60+", "where": {"age_band": ["60+"]}}]
    target = normalise(cfg)
    target["allocations"]["default"]["caller_id"] = {"D1": 1.0}
    out = limit_shift(cfg, target, 0.2)
    assert max_shift(cfg, out) <= 0.2 + 1e-9
    assert "age_band=60+" not in out["allocations"]     # still inherits default
    assert out["allocations"]["default"]["caller_id"]["D2"] < 0.5


def test_step_toward_moves_at_most_max_tv():
    cur, tgt = {"D1": 0.5, "D2": 0.5}, {"D1": 1.0}
    out = step_toward(cur, tgt, 0.2)
    assert tv(cur, out) <= 0.2 + 1e-9 and out["D1"] > 0.5


# ---------------------------------------------------------------- cohorts
def test_segment_ids_are_canonical_and_round_trip():
    a = Segment.of({"lang": ["ta", "hi"], "age_band": ["60+"]})
    b = Segment.of({"age_band": ["60+"], "lang": ["hi", "ta"]})
    assert a.id == b.id == "age_band=60+&lang=hi,ta"
    assert Segment.parse(a.id) == a
    assert a.matches({"age_band": "60+", "lang": "ta"}) and not a.matches({"age_band": "60+", "lang": "en"})
    assert Segment.of({"age_band": ["60+", "46-60"]}).covers(a)


def test_scheme_cells_are_a_disjoint_partition_and_recoverable_from_config():
    scheme = CohortScheme.of([("age_band", [["18-30"], ["31-45"], ["46-60", "60+"]]), ("city_tier", [["1"], ["2", "3"]])])
    cells = scheme.cells()
    assert len(cells) == 6
    lead = {"age_band": "60+", "city_tier": "3"}
    assert sum(c.matches(lead) for c in cells) == 1
    back = CohortScheme.from_config(scheme.config_cohorts())
    assert sorted(c.id for c in back.cells()) == sorted(c.id for c in cells)
    assert CohortScheme.from_config([{"id": "seniors", "where": {"age_band": ["60+"]}}]) is None  # foreign cohorts


# ---------------------------------------------------------------- stats
def test_wilson_and_binomial_tail():
    lo, hi = wilson(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60
    assert math.isclose(binom_sf(3, 3, 0.5), 0.125)
    assert binom_sf(0, 10, 0.3) == 1.0


def test_holm_exact_thresholds():
    assert holm({"a": 0.01, "b": 0.02, "c": 0.04}, 0.05) == {"a": True, "b": True, "c": True}
    assert holm({"a": 0.02, "b": 0.03}, 0.05) == {"a": True, "b": True}
    assert holm({"a": 0.03, "b": 0.04}, 0.05) == {"a": False, "b": False}


def test_stratified_comparison_removes_simpsons_paradox():
    # Arm A is better *within* each stratum, but was mostly run in the low-rate stratum.
    strata = [(3600, 1080, 400, 100), (400, 320, 3600, 2800)]   # (n_a, k_a, n_b, k_b)
    pooled_a = (1080 + 320) / 4000
    pooled_b = (100 + 2800) / 4000
    assert pooled_a < pooled_b                          # naive pooling says A is worse
    c = stratified_compare(strata)
    assert c.diff > 0 and c.p_better > 0.9              # stratified says A is better


def test_sprt_decides_both_ways():
    t = SPRT(0.015, 0.0375, 0.001, 0.01)
    assert t.decide(20, 300) == "h1"
    assert t.decide(2, 600) == "h0"
    assert t.decide(1, 20) == "continue"
