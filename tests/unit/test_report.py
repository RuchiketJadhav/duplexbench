"""M7-T04: every spec §10 item renders; --public drops clips and scenario ids; caveats fire.

`report/clips.py` belongs to another task: its `cut` is replaced by a fake module here.
"""
import sys
import types
from pathlib import Path

import pytest

from _report_fixture import PRIVATE_SCENARIO, build
from duplexbench.report import html as html_mod
from duplexbench.report.aggregate import aggregate
from duplexbench.report.run_card import build as build_card
from duplexbench.thresholds import Thresholds

TH = Thresholds()


@pytest.fixture
def fake_clips(monkeypatch):
    calls = []

    def cut(wav_path, t0_ms, t1_ms, events=(), *, out_dir=None, origin_ns=0):
        # same signature as report/clips.cut (ms, out_dir keyword)
        calls.append((wav_path, t0_ms, t1_ms, events, out_dir))
        d = Path(out_dir) if out_dir is not None else Path(wav_path).parent / "clips"
        d.mkdir(parents=True, exist_ok=True)
        out = d / f"clip_{int(t0_ms)}_{int(t1_ms)}.wav"
        out.write_bytes(b"RIFF0000WAVE")
        return out, [{"t_ms": (e["t_ns"]) / 1e6, "kind": e["kind"]} for e in events]

    mod = types.ModuleType("duplexbench.report.clips")
    mod.cut = cut
    monkeypatch.setitem(sys.modules, "duplexbench.report.clips", mod)
    return calls


@pytest.fixture
def db(tmp_path):
    p = build(tmp_path / "e.sqlite")
    # point the calls at a real file so clips can be "cut"
    import sqlite3
    wav = tmp_path / "call.wav"
    wav.write_bytes(b"RIFF")
    con = sqlite3.connect(p)
    con.execute("UPDATE calls SET wav_path = ?", (str(wav),))
    con.commit()
    con.close()
    return p


def _render(db, out, public, th=TH):
    return html_mod.report(db, "r1", out, th, public=public)


ANCHORS = [
    # 1 run card
    'id="run-card"', "simulator patience rule", "scenario-pack hash", "vendors that received audio",
    "network baseline", "instance type", "iteration counts", "reference ASR", "calibration",
    "rms_active_dbfs",
    # 2 profile
    'id="profile"', "pass_at_1", "95% CI", "<th>n</th>", "self-hosted",
    'class="marker m-classifier"', 'class="marker m-diagnostic"', "insufficient n",
    # 3 floor control
    'id="floor-control"', "Yield @1000 ms vs pooled Hold-while-speaking",
    "Hold-while-listening vs Response rate", "marker size = Response rate @5000 ms",
    # 4 latency tails
    'id="latency"', "model-level twin: t_ttfa_recv", "model-level twin: t_stop_model",
    "playout penalty", "backlog_ms", "p50 &#8594; p95",
    # 5 tasks
    'id="tasks"', "anchor_offset_in_utt_ms", "share &gt; 1 s", "domain: billing=",
    "bucket: 2000=", "Interrupted vs clean", "pass_pow_3", "unsupported_claim_rate",
    # 6 entities
    'id="entities"', "reference-ASR floor", "within floor: indistinguishable",
    # 7 frontier
    'id="frontier"', "Cost per connected minute vs Pass@1", "T_turn_gap_ear p50 vs Pass@1",
    'stroke-dasharray="4 4"', "Frontier systems (cost vs Pass@1): sysA",
    # 8 deltas
    'id="deltas"', "C1 − C0",
    # 9 clips
    'id="clips"', "<audio controls", 'id="clip-filter"',
    # 10 caveats
    'id="caveats"', "not validated", "no provider cancel event", "Low n:",
    "Pre-registered comparisons", "not distinguishable",
]


def test_every_section_renders(db, tmp_path, fake_clips):
    index = _render(db, tmp_path / "rep", public=False)
    text = index.read_text(encoding="utf-8")
    missing = [a for a in ANCHORS if a not in text]
    assert missing == []
    assert fake_clips, "clips were cut"
    assert (tmp_path / "rep" / "aggregate.json").exists()
    assert (tmp_path / "rep" / "run_card.json").exists()
    assert list((tmp_path / "rep" / "clips").iterdir())
    assert PRIVATE_SCENARIO in text          # private edition shows scenario ids


def test_no_composite_anywhere(db, tmp_path, fake_clips):
    text = _render(db, tmp_path / "rep", public=False).read_text(encoding="utf-8").lower()
    for w in ("composite", "overall score", "total score"):
        assert w not in text


def test_public_has_no_audio_and_no_private_ids(db, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("clips must not be cut under --public")
    mod = types.ModuleType("duplexbench.report.clips")
    mod.cut = boom
    monkeypatch.setitem(sys.modules, "duplexbench.report.clips", mod)
    out = tmp_path / "pub"
    text = _render(db, out, public=True).read_text(encoding="utf-8")
    assert "<audio" not in text and 'id="clips"' not in text
    assert PRIVATE_SCENARIO not in text
    js = (out / "aggregate.json").read_text(encoding="utf-8")
    assert PRIVATE_SCENARIO not in js and '"public": true' in js
    assert not (out / "clips").exists()
    for a in ('id="run-card"', 'id="profile"', 'id="floor-control"', 'id="caveats"'):
        assert a in text


def test_rerender_byte_identical(db, tmp_path, fake_clips):
    a = _render(db, tmp_path / "a", public=False).read_bytes()
    b = _render(db, tmp_path / "b", public=False).read_bytes()
    assert a == b
    assert (tmp_path / "a" / "aggregate.json").read_bytes() == \
        (tmp_path / "b" / "aggregate.json").read_bytes()


def test_render_without_clips_module_needed(db, tmp_path):
    agg = aggregate(db, "r1", TH)
    card = build_card(db, "r1", TH)
    text = html_mod.render(agg, card, tmp_path / "r", TH, clips=None).read_text(encoding="utf-8")
    assert "No failure clips." in text


def test_failure_candidates(db):
    c = html_mod.failure_clips(db, "r1", TH)
    classes = {x["failure_class"] for x in c}
    assert {"pass_at_1", "yield_rate", "unsupported_claim_rate"} <= classes
    y = [x for x in c if x["failure_class"] == "yield_rate" and x["scenario_id"] == "s4"
         and x["system_id"] == "sysA"]
    # window = overlay onset 1.0 s - hold window .. end 1.5 s + hold window, clamped at start
    assert y and y[0]["t0_ns"] == 0 and y[0]["t1_ns"] == 1_500_000_000 + TH.hold_window_ms * 1_000_000
    assert max(sum(1 for x in c if x["failure_class"] == k) for k in classes) <= html_mod.CLIPS_PER_CLASS


def test_pareto():
    pts = [{"label": "a", "x": 1.0, "y": 0.5}, {"label": "b", "x": 2.0, "y": 0.9},
           {"label": "c", "x": 2.0, "y": 0.4}, {"label": "d", "x": None, "y": 1.0}]
    assert [p["label"] for p in html_mod.pareto(pts)] == ["a", "b"]
