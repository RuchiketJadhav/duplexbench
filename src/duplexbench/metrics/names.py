"""The only metric names (BUILD_PLAN §2.11; spec §1.5 "the only names allowed", §4).

Every member carries `.spec` (the spec section that defines it) and `.headline`
(True only for the metrics in the spec §0 table's "Headline metrics" column).
Unsupported-claim rate is False until its classifier is validated (spec §0, §8);
diagnostics (spec §4.5) are never headline. `report/` raises on any name
outside this enum. Never add a member without a spec section (CLAUDE.md §6 item 6).
"""
from __future__ import annotations

from enum import StrEnum


class Metric(StrEnum):
    # Track A (spec §4.1, §1.5)
    yield_rate = "yield_rate"
    t_stop_ear = "t_stop_ear"
    talk_over_share = "talk_over_share"
    respond_resume = "respond_resume"
    t_respond_ear = "t_respond_ear"
    wait_compliance = "wait_compliance"
    hold_speaking_rate = "hold_speaking_rate"
    content_leakage_rate = "content_leakage_rate"
    hold_listening_rate = "hold_listening_rate"
    response_rate = "response_rate"
    t_turn_gap_ear = "t_turn_gap_ear"
    # Track C (spec §4.2)
    pass_at_1 = "pass_at_1"
    pass_pow_3 = "pass_pow_3"
    stale_action_rate = "stale_action_rate"
    preemptive_tool_rate = "preemptive_tool_rate"
    unsupported_claim_rate = "unsupported_claim_rate"
    policy_violation_rate = "policy_violation_rate"
    auth_bypass_rate = "auth_bypass_rate"
    tool_wait_pass = "tool_wait_pass"
    filler_count = "filler_count"
    filler_s = "filler_s"
    dead_air_in_tool_s = "dead_air_in_tool_s"
    repeated_prompt_rate = "repeated_prompt_rate"
    response_rate_per_turn = "response_rate_per_turn"
    call_outcome = "call_outcome"
    # Track D (spec §4.3)
    capture_first = "capture_first"
    capture_final = "capture_final"
    recovery_request_rate = "recovery_request_rate"
    recovery_success = "recovery_success"
    readback_presence = "readback_presence"
    readback_error_rate = "readback_error_rate"
    codec_penalty = "codec_penalty"
    # Latency taxonomy and diagnostics (spec §1.5, §4.5)
    t_ttfa_recv = "t_ttfa_recv"
    t_stop_model = "t_stop_model"
    t_tool_offset = "t_tool_offset"
    t_tool_to_info_ear = "t_tool_to_info_ear"
    t_dead_air = "t_dead_air"
    t_caller_think = "t_caller_think"
    backlog_ms = "backlog_ms"
    playout_penalty = "playout_penalty"
    tor = "tor"
    anchor_offset_in_utt_ms = "anchor_offset_in_utt_ms"
    # Track L (spec §4.4) and simulator diagnostics (spec §7)
    cost_per_min = "cost_per_min"
    error_rate_at_c = "error_rate_at_c"
    mishearing_rate = "mishearing_rate"
    regeneration_rate = "regeneration_rate"

    @property
    def spec(self) -> str:
        return _SPEC[self.value]

    @property
    def headline(self) -> bool:
        return self.value in _HEADLINE


_SPEC: dict[str, str] = {
    "yield_rate": "4.1",
    "t_stop_ear": "1.5, 4.1",
    "talk_over_share": "4.1",
    "respond_resume": "4.1",
    "t_respond_ear": "1.5, 4.1",
    "wait_compliance": "4.1",
    "hold_speaking_rate": "4.1",
    "content_leakage_rate": "4.1",
    "hold_listening_rate": "4.1",
    "response_rate": "4.1",
    "t_turn_gap_ear": "1.5, 4.1",
    "pass_at_1": "4.2",
    "pass_pow_3": "4.2",
    "stale_action_rate": "4.2",
    "preemptive_tool_rate": "4.2",
    "unsupported_claim_rate": "4.2, 8",
    "policy_violation_rate": "4.2",
    "auth_bypass_rate": "4.2",
    "tool_wait_pass": "4.2",
    "filler_count": "4.2",
    "filler_s": "4.2",
    "dead_air_in_tool_s": "1.5, 4.2",
    "repeated_prompt_rate": "4.2",
    "response_rate_per_turn": "4.2",
    "call_outcome": "4.2",
    "capture_first": "4.3",
    "capture_final": "4.3",
    "recovery_request_rate": "4.3",
    "recovery_success": "4.3",
    "readback_presence": "4.3",
    "readback_error_rate": "4.3",
    "codec_penalty": "4.3",
    "t_ttfa_recv": "1.5, 4.5",
    "t_stop_model": "1.5, 4.5",
    "t_tool_offset": "1.5, 4.2",
    "t_tool_to_info_ear": "1.5, 4.2",
    "t_dead_air": "1.5",
    "t_caller_think": "1.5, 7",
    "backlog_ms": "1.3, 4.5",
    "playout_penalty": "4.5",
    "tor": "4.5",
    "anchor_offset_in_utt_ms": "4.2 (D6)",
    "cost_per_min": "4.4",
    "error_rate_at_c": "4.4",
    "mishearing_rate": "7, 4.5",
    "regeneration_rate": "7, 4.5",
}

# Spec §0 table, "Headline metrics" column. Track L's ear-level latencies are
# T_turn_gap_ear, T_stop_ear, T_respond_ear, T_tool_to_info_ear, T_dead_air.
_HEADLINE: frozenset[str] = frozenset({
    # A
    "yield_rate", "hold_speaking_rate", "hold_listening_rate", "response_rate",
    "t_stop_ear", "t_turn_gap_ear",
    # C (unsupported_claim_rate joins only after validation, spec §8)
    "pass_at_1", "pass_pow_3", "stale_action_rate", "preemptive_tool_rate",
    "policy_violation_rate",
    # D (capture accuracy first attempt / final)
    "capture_first", "capture_final", "recovery_success", "readback_error_rate",
    # L
    "t_respond_ear", "t_tool_to_info_ear", "t_dead_air", "cost_per_min", "error_rate_at_c",
})

assert set(_SPEC) == {m.value for m in Metric}
assert _HEADLINE <= set(_SPEC)
