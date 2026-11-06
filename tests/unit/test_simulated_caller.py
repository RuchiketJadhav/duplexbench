"""M4-T10: simulated caller source under the VirtualClock (spec §7)."""
import numpy as np

from duplexbench.scenarios.schema import Scenario
from duplexbench.simulator.audio import CallerVoice
from duplexbench.simulator.caller_llm import END
from duplexbench.simulator.simulated_caller import SimulatedCaller
from duplexbench.stimuli.caller_source import CallerSource
from duplexbench.stimuli.fake_tts import FakeTTS
from duplexbench.thresholds import Thresholds

MS = 1_000_000
TH = Thresholds()
UTTS = ["hi I need help", "my order is late", "thanks bye"]


class FakeLLM:
    """Reply index = caller turns so far in the context; END after the script.
    Context-keyed so a discarded speculation does not consume a line."""
    prompt_hash = "ph-test"

    def __init__(self, utts=UTTS):
        self.utts = list(utts)
        self.calls = 0
        self.contexts = []

    async def next_utterance(self, ctx):
        self.calls += 1
        self.contexts.append(ctx)
        i = ctx.caller_turns
        return self.utts[i] if i < len(self.utts) else END


class FakePlayout:
    def __init__(self):
        self.subs = []

    def subscribe(self, cb):
        self.subs.append(cb)

    def emit(self, kind, t_ns):
        for cb in self.subs:
            cb(kind, t_ns // (20 * MS), t_ns)


class FakeTools:
    def __init__(self):
        self.subs = {}

    def subscribe(self, token, cb):
        self.subs.setdefault(token, []).append(cb)

    def emit(self, token, kind, t_ns):
        for cb in self.subs[token]:
            cb(kind, {"t_ns": t_ns, "name": "lookup"})


class Rows:
    def __init__(self):
        self.rows = []

    def put(self, table, row):
        assert table == "sim_turns"
        self.rows.append(row)


def _scenario():
    return Scenario.model_validate({
        "id": "c1", "version": "1", "split": "public-dev", "domain": "retail", "track": "C",
        "source": "simulated", "caller_voice": "v1", "persona": "p", "goal": "g",
        "steps": [{"id": "s1", "text": "order late"}]})


def _make(clock, store, **kw):
    voice = CallerVoice(FakeTTS(), "v1", 1.0, threaded=False)
    return SimulatedCaller(_scenario(), kw.pop("llm", FakeLLM()), voice, clock, TH, store,
                           run_id="r1", call_id="k1", rng_seed=7, **kw)


def _drive(run_virtual, agent_script, *, tools=None, overlap_at_ms=None, llm=None):
    """agent_script(clock, caller, playout, tools, turn_ends) runs as the fake agent."""
    store = Rows()
    playout = FakePlayout()

    async def main(clock):
        kw = {}
        if tools is not None:
            kw = {"tool_server": tools, "tool_token": "tok"}
        if llm is not None:
            kw["llm"] = llm
        caller = _make(clock, store, **kw)
        caller.attach(playout)
        frames, ends = [], []

        async def send():
            async for f in caller.frames():
                frames.append(f)

        async def agent():
            await agent_script(clock, caller, playout, tools, ends)

        a, b = clock.task(send()), clock.task(agent())
        await clock.idle(a)
        await clock.idle(b)
        return caller, frames, ends

    (caller, frames, ends), clock = run_virtual(main)
    return caller, frames, ends, store


async def _agent_turn(clock, caller, playout, start_ns, dur_ms, text="ok"):
    await clock.sleep_until_ns(start_ns)
    playout.emit("onset", start_ns)
    stop = start_ns + dur_ms * MS
    await clock.sleep_until_ns(stop)
    caller.on_agent_text(text)
    playout.emit("stop", stop)


def _reactive_agent(first_ns=100 * MS, dur_ms=1000, gap_ms=300):
    async def script(clock, caller, playout, tools, ends):
        await _agent_turn(clock, caller, playout, first_ns, dur_ms, "hello how can I help")
        async for step, t in caller.turn_ends():
            ends.append((step, t))
            await _agent_turn(clock, caller, playout, t + gap_ms * MS, dur_ms, f"reply {step}")
    return script


def test_is_caller_source(run_virtual):
    async def main(clock):
        return _make(clock, Rows())
    c, _ = run_virtual(main)
    assert isinstance(c, CallerSource)


def test_three_turns_then_end(run_virtual):
    caller, frames, ends, store = _drive(run_virtual, _reactive_agent())
    rows = store.rows
    assert [r["turn_idx"] for r in rows] == [0, 1, 2]
    assert [r["text"] for r in rows] == UTTS
    assert all(r["discarded"] == 0 and r["speculative"] == 1 for r in rows)
    assert all(r["prompt_hash"] == "ph-test" and r["run_id"] == "r1" and r["call_id"] == "k1"
               for r in rows)
    # think time = onset - agent silence start: at least eot + min floor delay
    lo, hi = TH.sim_floor_delay_ms
    for r in rows:
        assert TH.sim_eot_ms + lo <= r["think_ms"] <= TH.sim_eot_ms + hi + TH.frame_ms
        assert r["late_ms"] <= TH.frame_ms
    # frames paced exactly 20 ms, seq contiguous, END ended frames()
    dt = np.diff([f.t_ns for f in frames])
    assert (dt == TH.frame_ms * MS).all()
    assert [f.seq for f in frames] == list(range(len(frames)))
    assert [s for s, _ in ends] == ["sim0", "sim1", "sim2"]
    # the stream ends at the 4th floor (END), not later
    assert frames[-1].t_ns < ends[-1][1] + (TH.frame_ms * 10 + TH.sim_eot_ms + 2 * hi) * MS
    # silence frames are mu-law 0xff and carry no speech; speech frames carry the step id
    sil = [f for f in frames if not f.speech]
    assert sil and all(f.mulaw == b"\xff" * 160 for f in sil)
    sp = {f.step_id for f in frames if f.speech}
    assert sp == {"sim0", "sim1", "sim2"}
    # turn end = send time of the last speech frame of the turn
    for step, t in ends:
        assert max(f.t_ns for f in frames if f.step_id == step) == t
    # caller onset matches send_ns
    for r in rows:
        first = min(f.t_ns for f in frames if f.step_id == f"sim{r['turn_idx']}")
        assert first == r["send_ns"]
    assert [w for w, _ in caller.transcript] == ["agent", "caller"] * 3 + ["agent"]
    assert caller.flags == {}


def test_tool_pending_delays_floor(run_virtual):
    tools = FakeTools()
    result_ns = 3000 * MS

    async def script(clock, caller, playout, tools_, ends):
        await clock.sleep_until_ns(100 * MS)
        playout.emit("onset", 100 * MS)
        await clock.sleep_until_ns(900 * MS)
        tools_.emit("tok", "start", 900 * MS)
        await clock.sleep_until_ns(1100 * MS)
        playout.emit("stop", 1100 * MS)
        await clock.sleep_until_ns(result_ns)
        tools_.emit("tok", "result", result_ns)
        async for step, t in caller.turn_ends():
            ends.append((step, t))
            await _agent_turn(clock, caller, playout, t + 300 * MS, 500, "done")

    caller, frames, ends, store = _drive(run_virtual, script, tools=tools)
    first = store.rows[0]
    assert first["send_ns"] >= result_ns + TH.sim_patience_ms * MS
    assert first["send_ns"] < result_ns + (TH.sim_patience_ms + 2 * TH.frame_ms) * MS
    assert not any(f.speech for f in frames if f.t_ns < result_ns + TH.sim_patience_ms * MS)
    assert len([r for r in store.rows if not r["discarded"]]) == 3


def test_agent_resume_discards_speculation(run_virtual):
    async def script(clock, caller, playout, tools, ends):
        await _agent_turn(clock, caller, playout, 100 * MS, 1000, "hello")
        # resumes 500 ms into the silence: past sim_speculate_ms, before sim_eot_ms
        await _agent_turn(clock, caller, playout, 1600 * MS, 1000, "and also")
        async for step, t in caller.turn_ends():
            ends.append((step, t))
            await _agent_turn(clock, caller, playout, t + 300 * MS, 1000, "ok")

    caller, frames, ends, store = _drive(run_virtual, script)
    rows = store.rows
    assert [(r["turn_idx"], r["discarded"]) for r in rows] == [(0, 1), (0, 0), (1, 0), (2, 0)]
    assert rows[0]["text"] == UTTS[0] and rows[0]["send_ns"] is None
    assert rows[1]["send_ns"] >= (2600 + TH.sim_eot_ms) * MS
    assert caller.policy.discarded_speculations == 1
    assert caller.transcript[0] == ("agent", "hello and also")


def test_unscripted_overlap_flag(run_virtual):
    async def overlapping(clock, caller, playout, tools, ends):
        await _agent_turn(clock, caller, playout, 100 * MS, 1000, "hello")
        # wait for the caller's first speech, then talk over it
        while not caller.rows:
            await clock.sleep_until_ns(clock.now_ns() + 20 * MS)
        t0 = caller.rows[0]["send_ns"] + 100 * MS
        await _agent_turn(clock, caller, playout, t0, 200, "sorry go on")
        async for step, t in caller.turn_ends():
            ends.append((step, t))
            await _agent_turn(clock, caller, playout, t + 300 * MS, 1000, "ok")

    caller, frames, ends, store = _drive(run_virtual, overlapping)
    assert caller.flags == {"unscripted_overlap": 1}
    assert caller.policy.overlaps[0]["turn_idx"] == 0
    # the caller finished its utterance: sim0 speech is contiguous
    sim0 = [f.seq for f in frames if f.step_id == "sim0"]
    assert sim0 == list(range(sim0[0], sim0[-1] + 1))


def test_inject_mixes_overlay(run_virtual):
    clip = (np.sin(np.arange(800) / 5) * 3000).astype(np.int16)

    async def script(clock, caller, playout, tools, ends):
        await clock.sleep_until_ns(40 * MS)
        caller.inject(clip, -6.0, clock.now_ns())
        caller.close()

    caller, frames, ends, store = _drive(run_virtual, script)
    ov = [f for f in frames if f.overlay_id]
    assert len(ov) == 5 and all(f.speech for f in ov)
    assert ov[0].t_ns >= 40 * MS
    rms = np.sqrt(np.mean(np.concatenate([f.pcm for f in ov]).astype(float) ** 2))
    assert abs(20 * np.log10(rms / 32768) - (-26.0)) < 0.5
    assert store.rows == []
