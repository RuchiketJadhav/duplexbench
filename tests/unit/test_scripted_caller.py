"""M2-T06: scripted caller source under the VirtualClock."""
import math

import numpy as np
import pytest

from duplexbench.frames import Frame
from duplexbench.scenarios.schema import Scenario
from duplexbench.stimuli.scripted_caller import (CallerSourceLike, ScriptedCaller, dbfs,
                                                 resumption_key)
from duplexbench.thresholds import Thresholds

RATE = 8000
MS = 1_000_000
TH = Thresholds()


def _tone(ms, dbfs_=-20.0, f=300.0):
    t = np.arange(RATE * ms // 1000) / RATE
    return np.rint(10 ** (dbfs_ / 20) * 32768 * math.sqrt(2) * np.sin(2 * np.pi * f * t)
                   ).astype(np.int16)


def _scenario(steps, overlays=()):
    return Scenario.model_validate({
        "id": "t", "version": "1", "split": "public-dev", "domain": "x", "track": "A",
        "source": "scripted", "caller_voice": "v", "steps": list(steps),
        "overlays": list(overlays)})


EEND = {"id": "e", "cls": "E-END", "anchor_step": "s2", "placement": "during_agent",
        "offset_ms": 0, "text": "", "resumption_ms": 6000}


def _run(run_virtual, caller, on_frame=None):
    async def main(clock):
        frames, turns = [], []

        async def send():
            async for f in caller.frames():
                frames.append(f)
                if on_frame:
                    on_frame(caller, f)

        async def listen():
            async for te in caller.turn_ends():
                turns.append(te)

        a, b = clock.task(send()), clock.task(listen())
        await clock.idle(a)
        await clock.idle(b)
        return frames, turns

    return main


def test_two_steps_six_second_silence(run_virtual):
    sc = _scenario([{"id": "s1", "text": "a"}, {"id": "s2", "text": "b"}], [EEND])
    holder = {}

    def main(clock):
        c = ScriptedCaller(sc, {"s1": _tone(1000), "s2": _tone(1000)}, clock, TH)
        holder["c"] = c
        assert isinstance(c, CallerSourceLike)
        return _run(run_virtual, c)(clock)

    (frames, turns), clock = run_virtual(main)
    # s1 1.0 s | 6 s post-turn silence | s2 1.0 s | 6 s E-END silence
    assert turns == [("s1", 1000 * MS), ("s2", 8000 * MS)]
    last_speech = max(i for i, f in enumerate(frames) if np.any(f.pcm))
    assert last_speech + 1 == 400                       # 400 frames up to the last turn end
    assert len(frames) == 400 + 300                     # plus the trailing E-END silence
    assert all(isinstance(f, Frame) and f.src == "caller" for f in frames)
    assert [f.t_ns for f in frames[:3]] == [0, 20 * MS, 40 * MS]
    assert [f.seq for f in frames] == list(range(700))
    assert all(len(f.pcm) == 160 and len(f.mulaw) == 160 for f in frames)
    assert not np.any(frames[50].pcm) and not np.any(frames[349].pcm) and np.any(frames[350].pcm)
    assert clock.now_ns() == 699 * 20 * MS
    assert [s.kind for s in holder["c"].segments] == ["speech", "post_turn"] * 2


def test_pause_then_resumption(run_virtual):
    sc = _scenario([{"id": "s1", "text": "my number is five five five", "pause_ms": 900,
                     "resumption_text": "one two three four"}])
    clips = {"s1": _tone(1000), resumption_key("s1"): _tone(500)}
    (frames, turns), _ = run_virtual(
        lambda clock: _run(run_virtual, ScriptedCaller(sc, clips, clock, TH))(clock))
    active = [bool(np.any(f.pcm)) for f in frames]
    assert active[:50] == [True] * 50
    assert active[50:95] == [False] * 45                 # 900 ms pause
    assert active[95:120] == [True] * 25                 # 500 ms resumption clip
    assert not any(active[120:])
    assert turns == [("s1", (1000 + 900 + 500) * MS)]
    assert len(frames) == 120 + TH.eend_silence_ms // 20


def test_speech_end_ms_sets_turn_end(run_virtual):
    clip = np.concatenate([_tone(700), np.zeros(RATE * 50 // 1000, np.int16)])
    sc = _scenario([{"id": "s1", "text": "x", "speech_onset_ms": 0, "speech_end_ms": 700}])
    (_, turns), _ = run_virtual(
        lambda clock: _run(run_virtual, ScriptedCaller(sc, {"s1": clip}, clock, TH))(clock))
    assert turns == [("s1", 700 * MS)]


def test_injection_frames_and_level(run_virtual):
    sc = _scenario([{"id": "s1", "text": "a"}])
    rng = np.random.default_rng(7)
    ov = np.clip(rng.normal(0, 3000, RATE * 400 // 1000), -32768, 32767).astype(np.int16)
    got = {}

    def on_frame(c, f):
        if f.seq == 100:                                 # in the post-turn silence
            got["id"] = c.inject(ov, -6.0, t_ns=f.t_ns, overlay_id="o1")
            got["start"] = c.injected_at[got["id"]]
        if f.seq == 20:                                  # over the caller's own speech
            got["id2"] = c.inject(ov, -6.0)
            got["start2"] = c.injected_at[got["id2"]]

    (frames, _), _ = run_virtual(
        lambda clock: _run(run_virtual, ScriptedCaller(sc, {"s1": _tone(1000)}, clock, TH),
                           on_frame)(clock))
    assert got["start"] == 101 * 20 * MS and got["start2"] == 21 * 20 * MS
    assert got["id"] == "o1" and got["id2"] == "inj-0"           # CallerSource: inject -> id
    assert frames[101].overlay_id == "o1" and frames[101].speech
    assert frames[21].overlay_id == "inj-0" and frames[21].step_id == "s1"
    assert frames[100].overlay_id is None and not frames[100].speech
    assert not np.any(frames[100].pcm) and not np.any(frames[121].pcm)
    mixed = np.concatenate([f.pcm for f in frames[101:121]])          # 0.4 s = 20 frames
    assert abs(dbfs(mixed) - (-26.0)) < 1.0                           # -20 dBFS ref, -6 dB
    expect = ov.astype(np.float64) * (32768 * 10 ** (-26 / 20)) / math.sqrt(np.mean(
        ov.astype(np.float64) ** 2))
    assert np.max(np.abs(mixed - expect)) <= 1
    # summed over speech: mix - base == scaled overlay
    base = _tone(1000)[21 * 160:41 * 160].astype(np.float64)
    over = np.concatenate([f.pcm for f in frames[21:41]]).astype(np.float64) - base
    assert np.max(np.abs(over - expect)) <= 1
    assert np.array_equal(frames[20].pcm, _tone(1000)[20 * 160:21 * 160])   # not yet mixed


def test_injection_extends_past_script_end(run_virtual):
    sc = _scenario([{"id": "s1", "text": "a"}])
    th = Thresholds()
    ov = _tone(400)

    def on_frame(c, f):
        if f.seq == c.n_frames - 1:
            c.inject(ov, 0.0)

    (frames, _), _ = run_virtual(
        lambda clock: _run(run_virtual, ScriptedCaller(sc, {"s1": _tone(1000)}, clock, th),
                           on_frame)(clock))
    n = 50 + th.eend_silence_ms // 20
    assert len(frames) == n + 20 and np.any(frames[-1].pcm)


def test_missing_clips_refused():
    from duplexbench.clock import VirtualClock
    sc = _scenario([{"id": "s1", "text": "a", "pause_ms": 900, "resumption_text": "b"}])
    with pytest.raises(ValueError, match="no clip for step"):
        ScriptedCaller(sc, {}, VirtualClock(), TH)
    with pytest.raises(ValueError, match="resumption"):
        ScriptedCaller(sc, {"s1": _tone(100)}, VirtualClock(), TH)


def test_fixture_track_a_scenario_plays(run_virtual):
    from pathlib import Path

    import soundfile as sf

    from duplexbench.scenarios.loader import load_pack
    fix = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev"
    sc = next(s for s in load_pack(fix, check_tools=False).scenarios if s.id == "track_a_002")
    rd = lambda name: sf.read(str(fix / name), dtype="int16")[0]
    clips = {st.id: rd(st.clip) for st in sc.steps}
    clips[resumption_key("s1")] = rd(sc.steps[1].clip)          # any 8 kHz clip stands in
    (frames, turns), _ = run_virtual(
        lambda clock: _run(run_virtual, ScriptedCaller(sc, clips, clock, TH))(clock))
    # s1: 1300 ms clip (speech ends 1250) + 900 ms pause + 900 ms resumption -> 3100 ms
    assert turns[0] == ("s1", 3100 * MS)
    # s1 post-turn 6000 ms; s2 starts at 9100 ms, speech_end_ms 850
    assert turns[1] == ("s2", (9100 + 850) * MS)
    assert len(frames) == (9100 + 900 + 6000) // 20
