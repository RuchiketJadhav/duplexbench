"""TTS stimulus generation with word timestamps (BUILD_PLAN M2-T04; spec §3.5, CLAUDE.md §1).

Approve-first: synthesis is spend. `cli stimuli generate <pack_dir>` prints the
projected cost (characters x price) and refuses without `--yes`. No real vendor
client exists yet: the vendor is not chosen (docs/providers.md; CLAUDE.md §6 item 2
"which third parties may receive audio" is a human decision). Only `FakeTTS`
(stimuli/fake_tts.py) is wired, for tests and goldens.

    pcm, sr, words = synthesize("hold on", "voice-a", 1.0, client, cache_dir=Path(".tts-cache"))

Results are cached by sha256 of canonical JSON `[vendor, voice, rate, text]`:
`<cache_dir>/<key>.wav` (int16 mono) + `<key>.json` (words, sample rate, inputs).
A cache hit never calls the client.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import numpy as np
import soundfile as sf

from duplexbench.storage import canonical_json

_CHARS_PER_UNIT = 1000      # threshold-ok: prices are quoted per 1k characters


@dataclass(frozen=True)
class Word:
    text: str
    start_ms: int
    end_ms: int


@runtime_checkable
class TTSClient(Protocol):
    """A TTS vendor client. `rate` is the speaking rate (1.0 = normal), not a sample rate."""
    vendor: str

    def synthesize(self, text: str, voice: str, rate: float) -> tuple[np.ndarray, int, list[Word]]:
        """-> (int16 mono pcm, sample rate, word timestamps on the pcm timeline)."""
        ...


def cache_key(vendor: str, voice: str, rate: float, text: str) -> str:
    return hashlib.sha256(canonical_json([vendor, voice, float(rate), text]).encode()).hexdigest()


def synthesize(text: str, voice: str, rate: float, client: TTSClient,
               cache_dir: Path | str | None = None) -> tuple[np.ndarray, int, list[Word]]:
    """Synthesize through `client`, or return the cached result for the same inputs."""
    key = cache_key(client.vendor, voice, rate, text)
    if cache_dir is not None:
        d = Path(cache_dir)
        wav, meta = d / f"{key}.wav", d / f"{key}.json"
        if wav.is_file() and meta.is_file():
            pcm, sr = sf.read(str(wav), dtype="int16")
            m = json.loads(meta.read_text(encoding="utf-8"))
            return pcm, int(sr), [Word(**w) for w in m["words"]]
    pcm, sr, words = client.synthesize(text, voice, rate)
    pcm = np.asarray(pcm, dtype=np.int16).ravel()
    if cache_dir is not None:
        d.mkdir(parents=True, exist_ok=True)
        sf.write(str(wav), pcm, int(sr), subtype="PCM_16")
        meta.write_text(canonical_json({
            "vendor": client.vendor, "voice": voice, "rate": float(rate), "text": text,
            "sample_rate": int(sr), "words": [asdict(w) for w in words]}), encoding="utf-8")
    return pcm, int(sr), list(words)


def project_usd(texts: Iterable[str], usd_per_1k_chars: float) -> float:
    """Projected cost: total characters x price per 1k characters (BUILD_PLAN M2-T04)."""
    return sum(len(t) for t in texts) / _CHARS_PER_UNIT * float(usd_per_1k_chars)


@dataclass(frozen=True)
class TTSItem:
    voice: str
    rate: float
    text: str


def pack_items(scenarios) -> list[TTSItem]:
    """Every distinct (voice, rate, text) a scripted pack needs: step text, step
    resumption text, overlay text (overlay voice_id or the caller voice). Empty
    texts (P-PAUSE, E-END) are skipped."""
    seen: dict[tuple[str, float, str], TTSItem] = {}
    for s in scenarios:
        want = [(s.caller_voice, st.text) for st in s.steps]
        want += [(s.caller_voice, st.resumption_text) for st in s.steps if st.resumption_text]
        want += [(o.voice_id or s.caller_voice, o.text) for o in s.overlays]
        for voice, text in want:
            if text and text.strip():
                k = (voice, float(s.speaking_rate), text)
                seen.setdefault(k, TTSItem(*k))
    return list(seen.values())
