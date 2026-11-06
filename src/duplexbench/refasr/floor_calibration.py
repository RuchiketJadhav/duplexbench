"""Reference-ASR noise floor per entity type (BUILD_PLAN M5-T04; spec §4.3 read-back error floor).

    floors(asr, items, cond_chain, th) -> {type: accuracy}

`items` are `(type, expected_text, pcm8k)`: known-text read-backs. Each clip is
played through the channel's forward path (`ChannelChain.forward`, a fresh
chain per clip so loss/noise state never leaks between items), the line frames
are decoded (`ChannelChain.decode`: μ-law under a codec), and the result goes
to `asr.transcribe`. An item is correct when `contains_entity(normalize_entity(transcript),
expected_text, type)` holds: the same matching rule as `metrics/entities.score`
(whole tokens; phone / ZIP+4 / ID groups read back across commas, fix-21). The floor
of a type is its accuracy in [0, 1] (`metrics/entities.score` invalidates a
type whose floor is below the minimum).

`cond_chain`: a `Condition` (a `ChannelChain(cond, seed_fwd, seed_back)` is
built per item, seeds advanced by item index), or a zero-argument callable
returning a fresh `ChannelChain`.

`floor_mini(fake_tts)` builds the 20-item mini set (4 each of phone, email,
zip, dob, amount) with FakeTTS at its own rate, resampled to 8 kHz.

Not here yet (BUILD_PLAN M5-T04 full): >= 200 items, Wilson CI, the
`required_spoken` confirmation floor, results/floor-<ts>.json and `cli floor`.
"""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable, Iterable

import numpy as np

from duplexbench.channel.chain import ChannelChain, Condition
from duplexbench.channel.resample import Resampler
from duplexbench.metrics.entities import contains_entity
from duplexbench.metrics.normalize import normalize_entity
from duplexbench.refasr.engine import text_of

if TYPE_CHECKING:
    from duplexbench.refasr.engine import RefASR
    from duplexbench.thresholds import Thresholds

RATE = 8_000
Item = tuple[str, str, np.ndarray]

# (type, written expected form, spoken form for TTS)
MINI_SET: tuple[tuple[str, str, str], ...] = (
    ("phone", "415-555-0100", "four one five five five five zero one zero zero"),
    ("phone", "(212) 555-0187", "two one two five five five zero one eight seven"),
    ("phone", "650 555 0123", "six five zero five five five zero one two three"),
    ("phone", "303-555-0149", "three zero three five five five zero one four nine"),
    ("email", "dana.ruiz@example.com", "dana dot ruiz at example dot com"),
    ("email", "jlee@example.org", "j l e e at example dot org"),
    ("email", "sam_ortiz@example.net", "sam underscore ortiz at example dot net"),
    ("email", "kpatel@example.com", "k p a t e l at example dot com"),
    ("zip", "94105", "nine four one zero five"),
    ("zip", "10001", "one zero zero zero one"),
    ("zip", "60614", "six zero six one four"),
    ("zip", "02139", "zero two one three nine"),
    ("dob", "1988-04-02", "april second nineteen eighty eight"),
    ("dob", "1975-12-31", "december thirty first nineteen seventy five"),
    ("dob", "2001-07-04", "july fourth two thousand one"),
    ("dob", "1990-03-03", "march third nineteen ninety"),
    ("amount", "$42.10", "forty two dollars and ten cents"),
    ("amount", "$1,250.00", "one thousand two hundred fifty dollars"),
    ("amount", "$19.99", "nineteen dollars and ninety nine cents"),
    ("amount", "$7.50", "seven dollars and fifty cents"),
)


def _chain_factory(cond_chain: Any, i: int, seed_fwd: int, seed_back: int) -> ChannelChain:
    if isinstance(cond_chain, Condition):
        return ChannelChain(cond_chain, seed_fwd + i, seed_back + i)
    if callable(cond_chain):
        return cond_chain()
    raise TypeError("cond_chain must be a Condition or a callable returning a ChannelChain")


def through_channel(chain: ChannelChain, pcm8k: np.ndarray) -> np.ndarray:
    """Forward path: source pcm -> line frames -> decoded int16 at the chain rate."""
    x = np.asarray(pcm8k, dtype=np.int16).ravel()
    # pad one frame of silence so the last partial frame is emitted
    x = np.concatenate([x, np.zeros(chain.frame_len, np.int16)])
    frames = [chain.decode(b) for b in chain.forward(x, RATE, True)]
    return np.concatenate(frames) if frames else np.zeros(0, np.int16)


def correct(expected_text: str, hyp_text: str, typ: str | None = None) -> bool:
    """Same rule as `metrics/entities.contains_entity`, with the item type (group read-backs)."""
    return contains_entity(normalize_entity(hyp_text), expected_text, typ)


def floors(asr: "RefASR", items: Iterable[Item], cond_chain: Condition | Callable[[], ChannelChain],
           th: "Thresholds", *, seed_fwd: int = 0, seed_back: int = 1) -> dict[str, float]:
    """Per-type reference-ASR accuracy on known-text clips through the channel."""
    hits: dict[str, int] = defaultdict(int)
    n: dict[str, int] = defaultdict(int)
    for i, (typ, expected, pcm) in enumerate(items):
        heard = through_channel(_chain_factory(cond_chain, i, seed_fwd, seed_back), pcm)
        n[typ] += 1
        hits[typ] += correct(expected, text_of(asr.transcribe(heard)), typ)
    return {t: hits[t] / n[t] for t in n}


def floor_mini(fake_tts: Any, voice: str = "floor-mini", rate: float = 1.0) -> list[Item]:
    """The 20-item mini floor set, synthesized with `fake_tts` and resampled to 8 kHz."""
    out: list[Item] = []
    for typ, expected, spoken in MINI_SET:
        pcm, sr, _words = fake_tts.synthesize(spoken, voice, rate)
        if sr != RATE:
            rs = Resampler(sr, RATE)
            pcm = np.concatenate([rs.push(pcm), rs.flush()])
        out.append((typ, expected, np.asarray(pcm, dtype=np.int16)))
    return out
