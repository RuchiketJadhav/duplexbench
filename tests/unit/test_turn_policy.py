"""M4-T07: simulator turn policy on the VirtualClock (spec §7)."""
import pytest

from duplexbench.simulator.turn_policy import (UNSCRIPTED_OVERLAP, Speak, TurnPolicy, TurnState,
                                               Wait)
from duplexbench.thresholds import Thresholds

MS = 1_000_000
TH = Thresholds()


def _sim(run_virtual, events, seed=7, reply_ready_ms=0, until_ms=20_000):
    """events: [(t_ms, fn_name, *args)] applied by an agent task; the sim task takes the
    floor at the first Speak once its reply is ready. Returns (policy, first TurnRecord|None)."""

    async def main(clock):
        pol = TurnPolicy(clock, TH, rng_seed=seed)
        frame = TH.frame_ms * MS

        async def agent():
            for t_ms, fn, *args in events:
                await clock.sleep_until_ns(t_ms * MS)
                getattr(pol, fn)(*args)

        async def sim():
            while clock.now_ns() < until_ms * MS:
                a = pol.next_action()
                if isinstance(a, Speak) and clock.now_ns() >= reply_ready_ms * MS:
                    return pol.start_speaking()
                nxt = clock.now_ns() + frame
                if isinstance(a, Wait) and a.until_ns is not None:
                    nxt = min(nxt, a.until_ns)
                if isinstance(a, Speak):
                    nxt = min(nxt, reply_ready_ms * MS)
                await clock.sleep_until_ns(max(nxt, clock.now_ns() + 1))
            return None

        ag = clock.task(agent())
        rec = await clock.idle(clock.task(sim()))
        await clock.idle(ag)
        return pol, rec

    (pol, rec), _ = run_virtual(main)
    return pol, rec


@pytest.mark.parametrize("seed", range(10))
def test_agent_stops_at_zero_first_speech_in_window(run_virtual, seed):
    pol, rec = _sim(run_virtual, [(0, "on_agent_active"), (0, "on_agent_inactive")], seed=seed)
    assert 1000 * MS <= rec.start_ns <= 1500 * MS
    assert rec.speculative is True and rec.think_ms == rec.start_ns / MS
    assert pol.state is TurnState.SPEAKING


def test_resumption_at_400ms_cancels(run_virtual):
    ev = [(0, "on_agent_active"), (0, "on_agent_inactive"), (400, "on_agent_active"),
          (2000, "on_agent_inactive")]
    pol, rec = _sim(run_virtual, ev)
    assert pol.discarded_speculations == 1
    assert 3000 * MS <= rec.start_ns <= 3500 * MS          # timed from the second stop only


def test_tool_pending_blocks_until_result_plus_patience(run_virtual):
    ev = [(0, "on_agent_active"), (0, "on_agent_inactive"), (0, "tool_pending", True),
          (5000, "tool_pending", False)]
    pol, rec = _sim(run_virtual, ev)
    assert rec.start_ns >= 6500 * MS
    assert rec.start_ns == (5000 + TH.sim_patience_ms) * MS


def test_think_ms_logged_when_reply_late(run_virtual):
    pol, rec = _sim(run_virtual, [(0, "on_agent_active"), (0, "on_agent_inactive")],
                    reply_ready_ms=2500)
    assert rec.start_ns == 2500 * MS
    assert rec.think_ms == pytest.approx(2500.0)
    assert rec.late_ms == pytest.approx((rec.start_ns - rec.floor_ns) / MS) and rec.late_ms > 0


def test_agent_speech_while_speaking_is_unscripted_overlap(run_virtual):
    async def main(clock):
        pol = TurnPolicy(clock, TH, rng_seed=1)
        pol.on_agent_active(0)
        pol.on_agent_inactive(0)
        await clock.sleep_until_ns(2000 * MS)
        rec = pol.start_speaking()
        assert pol.on_agent_active(2200 * MS) == UNSCRIPTED_OVERLAP
        assert pol.state is TurnState.SPEAKING                 # caller finishes its utterance
        pol.on_agent_inactive(2600 * MS)
        pol.finish_speaking(3000 * MS)
        return pol, rec

    (pol, rec), _ = run_virtual(main)
    assert pol.overlaps == [{"t_ns": 2200 * MS, "turn_idx": 0, "tag": UNSCRIPTED_OVERLAP}]
    assert rec.end_ns == 3000 * MS
    # the agent's silence during our turn started the next end-of-turn timer
    assert pol.next_action(3000 * MS) == Wait((2600 + TH.sim_eot_ms) * MS)


def test_cannot_speak_before_floor_and_seeded_delay(run_virtual):
    async def main(clock):
        out = []
        for seed in (3, 3, 4):
            pol = TurnPolicy(clock, TH, rng_seed=seed)
            pol.on_agent_active(0)
            pol.on_agent_inactive(0)
            with pytest.raises(RuntimeError):
                pol.start_speaking(800 * MS)
            pol.poll(TH.sim_eot_ms * MS)
            out.append(pol.floor_ns())
        return out

    floors, _ = run_virtual(main)
    assert floors[0] == floors[1] != floors[2]
