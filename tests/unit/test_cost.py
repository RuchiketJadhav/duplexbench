"""metrics/cost.py (M3-T01): projection, actual cost from usage, price-file contract."""
from __future__ import annotations

import pytest

from duplexbench.metrics import cost
from duplexbench.scenarios.schema import Scenario
from duplexbench.systems import load_prices

PRICES = {"source_url": "https://example.test/pricing", "checked": "2026-11-04",
          "rates": {"audio_in": 32.0, "audio_in_cached": 0.4, "audio_out": 64.0,
                    "text_in": 4.0, "text_in_cached": 0.4, "text_out": 24.0},
          "projection": {"audio_in_tokens_per_min": 600, "audio_out_tokens_per_min": 1200,
                         "agent_talk_share": 0.5, "context_rebill_factor": 3.0,
                         "default_call_minutes": {"A": 1.0, "C": 3.0}}}


def _scn(sid="a1", track="A", steps=None, overlays=None) -> Scenario:
    return Scenario.model_validate({
        "id": sid, "version": "1", "split": "public-dev", "domain": "scheduling", "track": track,
        "source": "scripted", "caller_voice": "v", "speaking_rate": 1.0,
        "steps": steps or [{"id": "s1", "text": "hello there", "speech_end_ms": 1000}],
        "overlays": overlays or [], "conditions": ["C0"], "trials": 1, "control_twin": False})


def test_usd_per_min_hand_computed():
    # in: 600 tok x $32/M x 3 = 0.0576; out: 0.5 x 1200 x $64/M = 0.0384
    assert cost.usd_per_min(PRICES) == pytest.approx(0.096)
    assert cost.usd_per_min(None) == 0.0


def test_three_minute_track_a_call_projects_hand_value():
    assert cost.project_call_usd(3.0, PRICES) == pytest.approx(0.288)


def test_project_uses_minutes_map_then_track_default():
    a, c = _scn("a1", "A"), _scn("c1", "C")
    usd = cost.project([a, c], None, PRICES, {"a1": 2.0})
    assert usd == pytest.approx(2.0 * 0.096 + 3.0 * 0.096)
    assert cost.project([a], None, None) == 0.0
    assert cost.project([a], None, PRICES, calls_per_scenario=2) == pytest.approx(2 * 0.096)


def test_est_call_minutes_follows_script_geometry():
    s = _scn(steps=[{"id": "s1", "text": "a b", "speech_end_ms": 1200, "pause_ms": 900,
                     "resumption_text": "x y z"},
                    {"id": "s2", "text": "c", "speech_end_ms": 800}],
             overlays=[{"id": "e", "cls": "E-END", "anchor_step": "s2", "placement": "during_agent",
                        "offset_ms": 0, "text": "", "level_db": 0, "resumption_ms": 7000}])
    # s1: 1200 + 900 + 3 words at 150 wpm (1200 ms) + 4000; s2: 800 + 7000
    ms = 1200 + 900 + 1200 + 4000 + 800 + 7000
    assert cost.est_call_minutes(s, 4000) == pytest.approx(ms / 60000)


def test_actual_from_usage_with_cached_split():
    u = {"input_tokens": 1120, "output_tokens": 210,
         "input_token_details": {"audio_tokens": 1000, "text_tokens": 120, "cached_tokens": 600,
                                 "cached_tokens_details": {"audio_tokens": 500, "text_tokens": 100}},
         "output_token_details": {"audio_tokens": 200, "text_tokens": 10}}
    want = (500 * 32 + 500 * 0.4 + 20 * 4 + 100 * 0.4 + 200 * 64 + 10 * 24) / 1e6
    assert cost.price_per_call([u], PRICES) == pytest.approx(want)
    assert cost.price_per_call(u, PRICES) == pytest.approx(want)         # one payload
    assert cost.actual_call_usd({"usage": u}, PRICES) == pytest.approx(want)
    assert cost.price_per_call([u, u], PRICES) == pytest.approx(2 * want)


def test_missing_price_is_none_and_free_is_zero():
    u = {"input_tokens": 10, "input_token_details": {"audio_tokens": 10}}
    no_rate = {**PRICES, "rates": {"audio_out": 64.0}}
    assert cost.price_per_call([u], no_rate) is None
    img = {"input_tokens": 50, "input_token_details": {"audio_tokens": 10}}
    assert cost.price_per_call([img], PRICES) is None                    # unpriced image tokens
    assert cost.price_per_call(None, PRICES) is None
    assert cost.price_per_call([], PRICES) == 0.0
    assert cost.price_per_call([u], None) == 0.0
    per_min = {"usd_per_min": 0.08, "source_url": "x", "checked": "y"}
    assert cost.price_per_call([], per_min, minutes=2.5) == pytest.approx(0.2)
    assert cost.price_per_call([], per_min) is None


def test_price_file_is_real_and_dated():
    p = load_prices("openai_realtime")
    assert p["source_url"].startswith("https://developers.openai.com/")
    assert p["checked"] == "2026-11-04"
    assert p["rates"]["audio_in"] == 32.0 and p["rates"]["audio_out"] == 64.0
    assert p["model"] == "gpt-realtime-2.1"


def test_price_file_without_source_refused(tmp_path):
    from duplexbench.systems import SystemError_
    (tmp_path / "x.yaml").write_text("checked: '2026-11-04'\nrates: {}\n", encoding="utf-8")
    with pytest.raises(SystemError_, match="source_url"):
        load_prices("x", tmp_path)
