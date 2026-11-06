"""BUILD_PLAN §2.6: every [Proposed] number, its source and spec section."""
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from duplexbench.thresholds import DEFAULT_PATH, Thresholds, ThresholdsError

# BUILD_PLAN §2.6, verbatim defaults.
EXPECTED = {
    "rms_active_dbfs": -45, "inactivity_run_ms": 500, "jitter_buffer_ms": 60, "frame_ms": 20,
    "calib_event_tol_ms": 5, "calib_audio_tol_ms": 20, "loop_lag_p99_ms": 5,
    "pacing_slip_p99_ms": 10, "silero_disagree_ms": 40, "yield_budgets_ms": (500, 1000, 2000),
    "yield_headline_ms": 1000, "hold_window_ms": 2000, "hold_also_ms": 1000,
    "response_windows_ms": (2000, 5000), "respond_jaccard": 0.5, "leakage_window_ms": 10000,
    "contested_offset_ms": 300, "contested_pause_ms": 1500, "overlay_offsets_ms": (500, 2000),
    "wait_resume_ms": (3000, 6000), "pause_ms": (600, 1500), "eend_silence_ms": 6000,
    "dead_air_gap_ms": 2000, "call_timeout_ms": 480000, "stall_ms": 20000,
    "tool_latency_buckets_ms": (0, 2000, 5000), "repeat_prompt_words": 4, "sim_eot_ms": 700,
    "sim_speculate_ms": 300, "sim_floor_delay_ms": (300, 800), "sim_patience_ms": 1500,
    "sim_slow_caller_p90_ms": 1500, "sim_regen_max": 2, "sim_regen_review_rate": 0.10,
    "refasr_partial_p90_ms": 300, "refasr_rtf_max": 0.5, "kappa_min": 0.7, "flip_rate_max": 0.02,
    "bootstrap_resamples": 1000, "p99_min_n": 1000, "holm_max_comparisons": 6,
    "harness_invalid_max": 0.02, "ge_p": 0.0034, "ge_r": 0.1667, "ge_loss_in_bad": 1.0,
    "noise_snr_db": 15, "noise_drift_db": 3, "burst_per_min": 1.0, "burst_len_ms": (150, 400),
    "burst_level_db": -10, "muffle_share": 0.20, "muffle_cutoff_hz": 1200, "side_level_db": -6,
    "bg_level_db": -12, "tuning_iterations": 10,
}


def test_every_field_and_default():
    th = Thresholds()
    assert set(Thresholds.model_fields) == set(EXPECTED)
    for k, v in EXPECTED.items():
        assert getattr(th, k) == v, k


def test_sources():
    facts = {k for k in EXPECTED if Thresholds.meta(k).source == "Fact"}
    assert facts == {"loop_lag_p99_ms", "pacing_slip_p99_ms"}
    for k in EXPECTED:
        assert Thresholds.meta(k).spec, k


def test_yaml_matches_defaults():
    assert DEFAULT_PATH.exists()
    th = Thresholds.load(DEFAULT_PATH)
    assert th == Thresholds()
    assert th.hash() == Thresholds().hash()


def test_hash_changes_with_one_value(tmp_path):
    items = Thresholds().items()
    items["contested_offset_ms"]["value"] = 301
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump(items))
    assert Thresholds.load(p).hash() != Thresholds().hash()


def test_relabel_refused(tmp_path):
    items = Thresholds().items()
    items["inactivity_run_ms"]["source"] = "Fact"
    p = tmp_path / "t.yaml"
    p.write_text(yaml.safe_dump(items))
    with pytest.raises(ThresholdsError):
        Thresholds.load(p)


def test_unknown_field_refused(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("not_a_threshold: 3\n")
    with pytest.raises(ThresholdsError):
        Thresholds.load(p)


def test_bare_values_accepted_and_frozen(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("stall_ms: 20000\n")
    th = Thresholds.load(p)
    assert th.hash() == Thresholds().hash()
    with pytest.raises(ValidationError):
        th.stall_ms = 1  # type: ignore[misc]


def test_missing_explicit_path_errors(tmp_path):
    with pytest.raises(ThresholdsError):
        Thresholds.load(tmp_path / "nope.yaml")
