"""fix-34: `stimuli.scripted_caller.LeadIn`, the agent_first gate, on synthetic onset/stop times."""
from __future__ import annotations

from duplexbench.stimuli.scripted_caller import LeadIn
from duplexbench.thresholds import Thresholds

MS = 1_000_000
TH = Thresholds()           # eend_silence_ms 6000, stall_ms 20000


def test_waits_eend_silence_after_the_greeting_stop():
    g = LeadIn(TH)
    assert not g.ready(0)
    g.on_activity("onset", 25, 500 * MS)
    assert not g.ready(1000 * MS)                     # greeting playing
    g.on_activity("stop", 150, 3000 * MS)             # reported 500 ms late; carries the run start
    assert not g.ready(8980 * MS)                     # 5980 ms of silence
    assert g.ready(9000 * MS)                         # 6000 ms = th.eend_silence_ms
    assert g.flags(0) == {"lead_in": "agent_first", "greeting_end_ms": 3000.0,
                          "step1_start_ms": 9000.0, "greeting_utterances": 1,
                          "no_greeting": False}


def test_resumed_greeting_restarts_the_wait():
    g = LeadIn(TH)
    g.ready(0)
    g.on_activity("onset", 0, 500 * MS)
    g.on_activity("stop", 0, 2000 * MS)
    assert not g.ready(4000 * MS)
    g.on_activity("onset", 0, 4500 * MS)              # second part of the greeting
    assert not g.ready(8000 * MS)                     # 6 s after the first stop, but speaking
    g.on_activity("stop", 0, 6000 * MS)
    assert not g.ready(11980 * MS)
    assert g.ready(12000 * MS)
    f = g.flags(0)
    assert f["greeting_end_ms"] == 6000.0 and f["greeting_utterances"] == 2


def test_no_greeting_guard_is_stall_ms():
    g = LeadIn(TH)
    assert not g.ready(100 * MS)                      # lead-in starts at the first check
    assert not g.ready((100 + 19980) * MS)
    assert g.ready((100 + 20000) * MS)
    f = g.flags(0)
    assert f["no_greeting"] is True and f["greeting_end_ms"] is None
    assert f["step1_start_ms"] == 20100.0 and f["greeting_utterances"] == 0


def test_activity_after_step1_is_ignored():
    g = LeadIn(TH)
    g.ready(0)
    g.on_activity("onset", 0, 100 * MS)
    g.on_activity("stop", 0, 1000 * MS)
    assert g.ready(7000 * MS)
    g.on_activity("onset", 0, 9000 * MS)              # the reply to step 1
    assert g.flags(0)["greeting_utterances"] == 1
