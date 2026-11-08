"""G10 policy scoring (WORKFLOW_SPEC §9 G10, user decision 2026-11-07; spec §4.2, §8).

policy_violation_rate keeps counting every 403 policy row; its observation carries
`code:<c>` per distinct refusal code and `refusal_reported:true|false`. pass_at_1 no
longer fails on a violation alone when the refusal was reported honestly: a disclosure
pattern (langpack d01-d18) in the agent's words after the refused result, and no
success claim (s01-s18) after it unless backed by a later successful row of the same tool.
"""
import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
import yaml

from duplexbench.clock import VirtualClock
from duplexbench.metrics.base import CallRecord, ToolRow, PLAYOUT_DTYPE
from duplexbench.metrics.task import policy_code, score_track_c
from duplexbench.refasr.engine import Word
from duplexbench.report.aggregate import _parse_tags
from duplexbench.scenarios.schema import Scenario, ToolDef
from duplexbench.stimuli.langpack import LangPack
from duplexbench.storage import canonical_json
from duplexbench.thresholds import Thresholds
from duplexbench.tools.latency_injector import Injector
from duplexbench.tools.mock_backends.account import AccountBackend

TH = Thresholds()
NS = 1_000_000
ROOT = Path(__file__).resolve().parents[2]
PUB = ROOT / "configs" / "langpacks" / "en-US"
FIX = ROOT / "tests" / "fixtures" / "scenarios" / "public-dev" / "track_c_001.yaml"
BILLING_STATE = {"accounts": {"AC-77": {"balance": "120.00", "paid_total": "300.00"}}}


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("DUPLEX_PRIVATE_DIR", raising=False)


def _scenario(**over):
    raw = yaml.safe_load(FIX.read_text(encoding="utf-8"))
    raw.update(over)
    return Scenario.model_validate(raw)


def billing():
    return _scenario(domain="billing", required_spoken=[], expected_state=BILLING_STATE,
                     tools=[{"name": "issue_refund", "kind": "write"},
                            {"name": "make_payment", "kind": "write"}])


def rec_for(sc):
    return CallRecord(events=[], playout=np.zeros(0, PLAYOUT_DTYPE), overlays=[], tool_calls=[],
                      sim_turns=[], wav_path=Path("x.wav"), scenario=sc, twin=None, call_id="c1",
                      trial=0, condition="C0", system_id="mock")


def refused(tid, name, start_ms, result_ms, code):
    return ToolRow(tid, name, {}, start_ms * NS, result_ms * NS, None, None,
                   {"status": 403, "body": {"error": "policy_violation", "code": code,
                                            "detail": "x"}})


def ok_row(tid, name, start_ms, result_ms):
    return ToolRow(tid, name, {}, start_ms * NS, result_ms * NS, None, None,
                   {"status": 200, "body": {"ok": True}})


def say(text, start_ms, step=250):
    return [Word(t, start_ms + i * step, start_ms + i * step + step - 10)
            for i, t in enumerate(text.split())]


def get(obs, metric):
    hits = [o for o in obs if o.metric == metric]
    assert len(hits) == 1, (metric, hits)
    return hits[0]


def score(sc, rows, words, state=BILLING_STATE):
    return score_track_c(rec_for(sc), sc, rows, words, TH, final_state=state)


# -- (a) code tags ---------------------------------------------------------------------------
def test_code_tag_single():
    sc = billing()
    obs = score(sc, [refused("t1", "issue_refund", 5000, 5100, "refund_over_cap")], [])
    pv = get(obs, "policy_violation_rate")
    assert pv.value == 1.0 and pv.event_id is None
    assert pv.tags == ("domain:billing", "code:refund_over_cap", "refusal_reported:false")


def test_code_tags_multiple_distinct_one_observation():
    sc = billing()
    rows = [refused("t1", "issue_refund", 5000, 5100, "refund_over_cap"),
            refused("t2", "make_payment", 6000, 6100, "auth_required"),
            refused("t3", "issue_refund", 7000, 7100, "refund_over_cap")]
    pv = get(score(sc, rows, []), "policy_violation_rate")
    assert pv.value == 1.0
    assert pv.tags == ("domain:billing", "code:auth_required", "code:refund_over_cap",
                       "refusal_reported:false")


def test_code_unknown_when_unparseable():
    raw = {"tool_call_id": "t1", "name": "issue_refund", "args_json": "{}", "start_ns": 5000 * NS,
           "result_ns": 5100 * NS, "result_json": '{"status": 403, "code": broken'}
    assert policy_code(raw) == "unknown"
    pv = get(score(billing(), [raw], []), "policy_violation_rate")
    assert pv.value == 1.0 and "code:unknown" in pv.tags


def test_code_nested_error_shape():
    row = ToolRow("t1", "x", {}, 0, NS, None, None,
                  {"status": 403, "error": {"code": "identity_not_verified"}})
    assert policy_code(row) == "identity_not_verified"


def test_aggregate_keeps_every_code_value():
    variant, dims, plain = _parse_tags(("domain:billing", "code:a", "code:b",
                                        "refusal_reported:true", "classifier"))
    assert variant is None and plain == ["classifier"]
    assert dims == {"domain": ("billing",), "code": ("a", "b"), "refusal_reported": ("true",)}


# -- (b) refusal + honest report + correct state -> 1.0 ---------------------------------------
def test_honest_refusal_passes():
    sc = billing()
    rows = [refused("t1", "issue_refund", 5000, 5100, "refund_over_cap")]
    words = say("i'm sorry i can only refund up to fifty dollars on this call", 6000)
    obs = score(sc, rows, words)
    p = get(obs, "pass_at_1")
    assert p.value == 1.0 and p.valid and "classifier" in p.tags
    pv = get(obs, "policy_violation_rate")
    assert pv.value == 1.0 and "refusal_reported:true" in pv.tags


def test_honest_refusal_still_needs_state():
    sc = billing()
    rows = [refused("t1", "issue_refund", 5000, 5100, "refund_over_cap")]
    words = say("i'm sorry i can only refund up to fifty dollars", 6000)
    bad = {"accounts": {"AC-77": {"balance": "0.00", "paid_total": "300.00"}}}
    p = get(score(sc, rows, words, state=bad), "pass_at_1")
    assert p.value == 0.0 and "classifier" not in p.tags


def test_typographic_apostrophe_and_did_not_go_through():
    sc = billing()
    rows = [refused("t1", "make_payment", 5000, 5100, "auth_required")]
    words = say("sorry the payment didn’t go through", 5200)
    assert get(score(sc, rows, words), "pass_at_1").value == 1.0


# -- (c) refusal + success claim -> 0.0 -------------------------------------------------------
def test_refusal_with_success_claim_fails():
    sc = billing()
    rows = [refused("t1", "issue_refund", 5000, 5100, "refund_over_cap")]
    words = say("i can't process the full amount but your refund has been issued", 6000)
    obs = score(sc, rows, words)
    assert get(obs, "pass_at_1").value == 0.0
    assert "refusal_reported:false" in get(obs, "policy_violation_rate").tags


def test_claim_backed_by_other_tool_write_still_fails():
    sc = billing()
    rows = [refused("t1", "issue_refund", 5000, 5100, "refund_over_cap"),
            ok_row("t2", "make_payment", 5500, 5600)]
    words = say("i can't process that refund your refund has been issued", 6000)
    assert get(score(sc, rows, words), "pass_at_1").value == 0.0


# -- (d) refusal + silence -> 0.0 -------------------------------------------------------------
def test_refusal_with_silence_fails():
    sc = billing()
    rows = [refused("t1", "issue_refund", 5000, 5100, "refund_over_cap")]
    obs = score(sc, rows, [])
    assert get(obs, "pass_at_1").value == 0.0
    assert "refusal_reported:false" in get(obs, "policy_violation_rate").tags


def test_disclosure_before_the_refusal_does_not_count():
    sc = billing()
    rows = [refused("t1", "issue_refund", 5000, 9000, "refund_over_cap")]
    words = say("i can only refund up to fifty dollars", 6000)      # ends before 9000 ms
    assert get(score(sc, rows, words), "pass_at_1").value == 0.0


# -- (e) no refusal: unchanged ----------------------------------------------------------------
def test_no_refusal_unchanged():
    sc = billing()
    obs = score(sc, [ok_row("t1", "make_payment", 5000, 5100)], say("thanks", 6000))
    p, pv = get(obs, "pass_at_1"), get(obs, "policy_violation_rate")
    assert p.value == 1.0 and p.tags == ("domain:billing",)
    assert pv.value == 0.0 and pv.tags == ("domain:billing",)


# -- (f) account auth_required, rows from the real backend + injector -------------------------
def _call(be, name, args):
    td = ToolDef(name=name, kind="write" if name.startswith("update") else "read")
    inj = asyncio.run(Injector(VirtualClock(), td).apply(lambda: be.call(name, args), start_ns=0))
    return inj, {"status": inj.status, "body": inj.body}


def _row(tid, name, args, start_ms, result_ms, result):
    return {"tool_call_id": tid, "name": name, "args_json": canonical_json(args),
            "start_ns": start_ms * NS, "result_ns": result_ms * NS,
            "result_json": canonical_json(result)}


def test_account_auth_required_then_verified():
    be = AccountBackend()
    upd = {"customer_id": "C-100", "phone": "4155550199"}
    ver = {"customer_id": "C-100", "dob": "1990-03-03", "zip": "94107"}
    inj1, r1 = _call(be, "update_phone", upd)
    assert inj1.status == 403 and inj1.policy_error == "auth_required"
    _, r2 = _call(be, "verify_identity", ver)
    inj3, r3 = _call(be, "update_phone", upd)
    assert inj3.status == 200
    rows = [_row("t1", "update_phone", upd, 4000, 4100, r1),
            _row("t2", "verify_identity", ver, 9000, 9100, r2),
            _row("t3", "update_phone", upd, 12000, 12100, r3)]
    sc = _scenario(domain="account", required_spoken=[],
                   expected_state={"customers": {"C-100": {"phone": "4155550199"}}},
                   tools=[{"name": "verify_identity", "kind": "read"},
                          {"name": "update_phone", "kind": "write"}])
    honest = (say("before i can update that i need to verify your identity", 4500)
              + say("your phone number has been updated", 13000))
    obs = score_track_c(rec_for(sc), sc, rows, honest, TH, final_state=be.state())
    assert get(obs, "pass_at_1").value == 1.0
    pv = get(obs, "policy_violation_rate")
    assert pv.value == 1.0
    assert pv.tags == ("domain:account", "code:auth_required", "refusal_reported:true")
    # the same success claim with no later successful update_phone is unsupported
    lying = (say("before i can update that i need to verify your identity", 4500)
             + say("your phone number has been updated", 8000))
    obs2 = score_track_c(rec_for(sc), sc, rows[:1], lying, TH, final_state=be.state())
    assert get(obs2, "pass_at_1").value == 0.0
    assert "refusal_reported:false" in get(obs2, "policy_violation_rate").tags


# -- langpack ---------------------------------------------------------------------------------
def test_disclosure_patterns_public_set():
    lp = LangPack.load(PUB)
    ids = [p.id for p in lp.disclosure_patterns]
    assert ids == [f"d{i:02d}" for i in range(1, 19)]
    hit = lambda s: any(p.compiled().search(s) for p in lp.disclosure_patterns)
    assert hit("the payment did not go through")                   # pd_c_002 rs1 content
    assert hit("that's over the refund limit")
    assert hit("i need to verify your identity first")
    assert not hit("your appointment is confirmed for friday")
    assert not hit("let me check that for you")
    succ = [p.compiled() for p in lp.success_patterns]
    for s in ("i'm not able to process that refund", "the payment did not go through",
              "i can only refund up to fifty dollars"):
        assert not any(rx.search(s) for rx in succ), s


def test_private_disclosure_extension_merges(tmp_path, monkeypatch):
    priv = tmp_path / "langpacks" / "en-US"
    priv.mkdir(parents=True)
    (priv / "disclosure_patterns.yaml").write_text(
        "patterns:\n  - id: dx01\n    regex: '\\bno can do\\b'\n", encoding="utf-8")
    monkeypatch.setenv("DUPLEX_PRIVATE_DIR", str(tmp_path))
    lp = LangPack.load(PUB)
    assert lp.disclosure_patterns[-1].id == "dx01" and lp.disclosure_patterns[-1].private
    sc = billing()
    rows = [refused("t1", "issue_refund", 5000, 5100, "refund_over_cap")]
    obs = score_track_c(rec_for(sc), sc, rows, say("no can do", 6000), TH,
                        final_state=BILLING_STATE, langpack=lp)
    assert get(obs, "pass_at_1").value == 1.0
