"""Every [Proposed] number of the spec, in one Pydantic model (BUILD_PLAN §2.6; CLAUDE.md §6 item 4).

This is the ONLY file under `src/duplexbench/` allowed to contain these numbers
as literals (`tests/unit/test_thresholds_guard.py` enforces it). Other modules
take a `Thresholds` instance and read attributes:

    th = Thresholds.load()            # configs/thresholds.yaml, defaults otherwise
    th.inactivity_run_ms              # -> 500 (the value itself)
    Thresholds.meta("inactivity_run_ms")  # -> Meta(source="Proposed", spec="1.2")

Each field is a (value, source) pair: the value is the attribute; its source
(`Proposed` or `Fact`) and spec section are fixed per field in `_META` and
declared in the YAML next to the value. A YAML that relabels a source or spec
section is refused, so a default cannot silently become a "Fact".
Defaults are the spec defaults; never change one without a decision recorded
in docs/decisions.md.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict

from duplexbench.storage import canonical_json

Source = Literal["Proposed", "Fact"]
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "configs" / "thresholds.yaml"


class Meta(NamedTuple):
    source: Source
    spec: str


class ThresholdsError(ValueError):
    pass


class Thresholds(BaseModel):
    """Spec defaults; section in the trailing comment and in `_META`."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)

    # §1.2 ground-truth anchors / activity
    rms_active_dbfs: float = -45
    inactivity_run_ms: int = 500
    # §1.3 playout
    jitter_buffer_ms: int = 60
    frame_ms: int = 20
    # §1.4 calibration
    calib_event_tol_ms: int = 5
    calib_audio_tol_ms: int = 20
    loop_lag_p99_ms: int = 5
    pacing_slip_p99_ms: int = 10
    silero_disagree_ms: int = 40
    # §4.1 Track A
    yield_budgets_ms: tuple[int, ...] = (500, 1000, 2000)
    yield_headline_ms: int = 1000
    hold_window_ms: int = 2000
    hold_also_ms: int = 1000
    response_windows_ms: tuple[int, ...] = (2000, 5000)
    respond_jaccard: float = 0.5
    leakage_window_ms: int = 10000
    # §3.3 contested bucket
    contested_offset_ms: int = 300
    contested_pause_ms: int = 1500
    # §3.2 overlay geometry
    overlay_offsets_ms: tuple[int, ...] = (500, 2000)
    wait_resume_ms: tuple[int, int] = (3000, 6000)
    pause_ms: tuple[int, int] = (600, 1500)
    eend_silence_ms: int = 6000
    # §1.5 latency taxonomy
    dead_air_gap_ms: int = 2000
    # §4.2 Track C
    call_timeout_ms: int = 480000
    stall_ms: int = 20000
    tool_latency_buckets_ms: tuple[int, ...] = (0, 2000, 5000)
    repeat_prompt_words: int = 4
    # §7 simulated caller
    sim_eot_ms: int = 700
    sim_speculate_ms: int = 300
    sim_floor_delay_ms: tuple[int, int] = (300, 800)
    sim_patience_ms: int = 1500
    sim_slow_caller_p90_ms: int = 1500
    sim_regen_max: int = 2
    sim_regen_review_rate: float = 0.10
    # §1.4 + eng review D4: reference-ASR calibration stage
    refasr_partial_p90_ms: int = 700          # TTFS p90, = sim_eot_ms (B22)
    refasr_rtf_max: float = 0.5
    # §8 classifiers
    kappa_min: float = 0.7
    flip_rate_max: float = 0.02
    # §9.1 / §4.4 statistics
    bootstrap_resamples: int = 1000
    p99_min_n: int = 1000
    holm_max_comparisons: int = 6
    # BUILD_PLAN §1 row 9 (M8 acceptance)
    harness_invalid_max: float = 0.02
    # §5 channel model (C1)
    ge_p: float = 0.0034
    ge_r: float = 0.1667
    ge_loss_in_bad: float = 1.0
    noise_snr_db: float = 15
    noise_drift_db: float = 3
    burst_per_min: float = 1.0
    burst_len_ms: tuple[int, int] = (150, 400)
    burst_level_db: float = -10
    muffle_share: float = 0.20
    muffle_cutoff_hz: int = 1200
    # §3.2 overlay levels
    side_level_db: float = -6
    bg_level_db: float = -12
    # §2.3 fairness: tuning budget
    tuning_iterations: int = 10

    # -- metadata -----------------------------------------------------------
    @classmethod
    def meta(cls, name: str) -> Meta:
        return _META[name]

    def items(self) -> dict[str, dict[str, Any]]:
        """name -> {value, source, spec}; the shape written to the YAML."""
        out: dict[str, dict[str, Any]] = {}
        for name in type(self).model_fields:
            v = getattr(self, name)
            m = _META[name]
            out[name] = {"value": list(v) if isinstance(v, tuple) else v,
                         "source": m.source, "spec": m.spec}
        return out

    def hash(self) -> str:
        """sha256 of the canonical JSON of every (value, source, spec)."""
        return hashlib.sha256(canonical_json(self.items()).encode()).hexdigest()

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Thresholds":
        """Load a thresholds YAML; missing file (default path only) -> spec defaults.

        Each entry is either a bare value or `{value, source, spec}`; when
        source/spec are given they must equal the field's declared metadata.
        """
        p = Path(path) if path is not None else DEFAULT_PATH
        if not p.exists():
            if path is None:
                return cls()
            raise ThresholdsError(f"thresholds file not found: {p}")
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ThresholdsError(f"{p}: top level must be a mapping")
        values: dict[str, Any] = {}
        for name, entry in raw.items():
            if name not in _META:
                raise ThresholdsError(f"{p}: unknown threshold {name!r}")
            if isinstance(entry, dict):
                m = _META[name]
                if entry.get("source", m.source) != m.source:
                    raise ThresholdsError(f"{name}: source {entry['source']!r} != declared {m.source!r}")
                if str(entry.get("spec", m.spec)) != m.spec:
                    raise ThresholdsError(f"{name}: spec {entry['spec']!r} != declared {m.spec!r}")
                if "value" not in entry:
                    raise ThresholdsError(f"{name}: missing value")
                values[name] = entry["value"]
            else:
                values[name] = entry
        return cls(**values)

    def dump_yaml(self) -> str:
        return yaml.safe_dump(self.items(), sort_keys=False)


_P: Source = "Proposed"
_F: Source = "Fact"
_META: dict[str, Meta] = {
    "rms_active_dbfs": Meta(_P, "1.2"),
    "inactivity_run_ms": Meta(_P, "1.2"),
    "jitter_buffer_ms": Meta(_P, "1.3"),
    "frame_ms": Meta(_P, "1.3"),
    "calib_event_tol_ms": Meta(_P, "1.4"),
    "calib_audio_tol_ms": Meta(_P, "1.4"),
    "loop_lag_p99_ms": Meta(_F, "1.4"),
    "pacing_slip_p99_ms": Meta(_F, "1.4"),
    "silero_disagree_ms": Meta(_P, "1.4"),
    "yield_budgets_ms": Meta(_P, "4.1"),
    "yield_headline_ms": Meta(_P, "4.1"),
    "hold_window_ms": Meta(_P, "4.1"),
    "hold_also_ms": Meta(_P, "4.1"),
    "response_windows_ms": Meta(_P, "4.1"),
    "respond_jaccard": Meta(_P, "4.1"),
    "leakage_window_ms": Meta(_P, "4.1"),
    "contested_offset_ms": Meta(_P, "3.3"),
    "contested_pause_ms": Meta(_P, "3.3"),
    "overlay_offsets_ms": Meta(_P, "3.2"),
    "wait_resume_ms": Meta(_P, "3.2"),
    "pause_ms": Meta(_P, "3.2"),
    "eend_silence_ms": Meta(_P, "3.2"),
    "dead_air_gap_ms": Meta(_P, "1.5"),
    "call_timeout_ms": Meta(_P, "4.2"),
    "stall_ms": Meta(_P, "4.2"),
    "tool_latency_buckets_ms": Meta(_P, "4.2"),
    "repeat_prompt_words": Meta(_P, "4.2"),
    "sim_eot_ms": Meta(_P, "7"),
    "sim_speculate_ms": Meta(_P, "7"),
    "sim_floor_delay_ms": Meta(_P, "7"),
    "sim_patience_ms": Meta(_P, "7"),
    "sim_slow_caller_p90_ms": Meta(_P, "7"),
    "sim_regen_max": Meta(_P, "7, D-review"),
    "sim_regen_review_rate": Meta(_P, "7"),
    "refasr_partial_p90_ms": Meta(_P, "1.4, D4"),
    "refasr_rtf_max": Meta(_P, "1.4, D4"),
    "kappa_min": Meta(_P, "8"),
    "flip_rate_max": Meta(_P, "8"),
    "bootstrap_resamples": Meta(_P, "9.1"),
    "p99_min_n": Meta(_P, "4.4"),
    "holm_max_comparisons": Meta(_P, "9.1"),
    "harness_invalid_max": Meta(_P, "M8"),
    "ge_p": Meta(_P, "5"),
    "ge_r": Meta(_P, "5"),
    "ge_loss_in_bad": Meta(_P, "5"),
    "noise_snr_db": Meta(_P, "5"),
    "noise_drift_db": Meta(_P, "5"),
    "burst_per_min": Meta(_P, "5"),
    "burst_len_ms": Meta(_P, "5"),
    "burst_level_db": Meta(_P, "5"),
    "muffle_share": Meta(_P, "5"),
    "muffle_cutoff_hz": Meta(_P, "5"),
    "side_level_db": Meta(_P, "3.2"),
    "bg_level_db": Meta(_P, "3.2"),
    "tuning_iterations": Meta(_P, "2.3"),
}
assert set(_META) == set(Thresholds.model_fields), "every threshold needs (source, spec)"
