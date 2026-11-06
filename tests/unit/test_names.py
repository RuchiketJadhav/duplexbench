"""BUILD_PLAN §2.11: the only metric names, with spec section and headline flag."""
from duplexbench.metrics.names import Metric

EXPECTED = [
    "yield_rate", "t_stop_ear", "talk_over_share", "respond_resume", "t_respond_ear",
    "wait_compliance", "hold_speaking_rate", "content_leakage_rate", "hold_listening_rate",
    "response_rate", "t_turn_gap_ear", "pass_at_1", "pass_pow_3", "stale_action_rate",
    "preemptive_tool_rate", "unsupported_claim_rate", "policy_violation_rate",
    "auth_bypass_rate", "tool_wait_pass", "filler_count", "filler_s", "dead_air_in_tool_s",
    "repeated_prompt_rate", "response_rate_per_turn", "call_outcome", "capture_first",
    "capture_final", "recovery_request_rate", "recovery_success", "readback_presence",
    "readback_error_rate", "codec_penalty", "t_ttfa_recv", "t_stop_model", "t_tool_offset",
    "t_tool_to_info_ear", "t_dead_air", "t_caller_think", "backlog_ms", "playout_penalty",
    "tor", "anchor_offset_in_utt_ms", "cost_per_min", "error_rate_at_c", "mishearing_rate",
    "regeneration_rate",
]

# Spec §0 "Headline metrics" column.
HEADLINE = {
    "yield_rate", "hold_speaking_rate", "hold_listening_rate", "response_rate", "t_stop_ear",
    "t_turn_gap_ear", "pass_at_1", "pass_pow_3", "stale_action_rate", "preemptive_tool_rate",
    "policy_violation_rate", "capture_first", "capture_final", "recovery_success",
    "readback_error_rate", "t_respond_ear", "t_tool_to_info_ear", "t_dead_air", "cost_per_min",
    "error_rate_at_c",
}


def test_exact_members():
    assert [m.value for m in Metric] == EXPECTED


def test_strenum_values_equal_names():
    for m in Metric:
        assert m == m.name and str(m) == m.name


def test_every_member_has_spec_section():
    for m in Metric:
        assert m.spec and m.spec[0].isdigit(), m


def test_headline_flags():
    assert {m.value for m in Metric if m.headline} == HEADLINE


def test_unvalidated_and_diagnostics_not_headline():
    for name in ("unsupported_claim_rate", "tor", "t_ttfa_recv", "t_stop_model", "backlog_ms",
                 "playout_penalty", "anchor_offset_in_utt_ms", "t_caller_think",
                 "mishearing_rate", "regeneration_rate"):
        assert not Metric(name).headline, name
