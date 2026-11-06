"""Channel conditions and the two-direction channel chain (spec §5; BUILD_PLAN M1-T07).

Forward (caller -> agent), spec §5 order:

    source pcm -> effects (noise bed, bursts, muffle) -> band-limit (C1) and resample
    to the line rate -> μ-law encode -> 20 ms frames -> Gilbert-Elliott loss with
    repeat-previous concealment -> delivery (μ-law bytes, or PCM16 when no codec)

Return (agent -> ear): provider pcm -> resample to the line rate -> μ-law ->
20 ms frames -> GE loss (independent seed) -> decode -> playout. No acoustic
effects on the return path (spec §5).

A `Condition` is built by `load_condition(name, th)`: the YAML in
`configs/conditions/<name>.yaml` says which stages are on (rate, codec, effects);
every numeric parameter comes from the `Thresholds` instance, never from a literal.

Streaming and alignment. `forward` is called with any chunk (the runner passes
one 20 ms frame at a time). The two linear-phase FIRs that need look-ahead (the
G.712 band-limit and the muffle low-pass, C1 only) hold back their group delay
and stay sample-aligned: line frame j carries exactly the samples of input frame j,
and it is completed (yielded) `delay_frames` = ceil(hold-back / frame) calls later.
The runner pairs each yielded frame with the input frame `delay_frames` back. Under C0 and W with a
source already at the line rate, `delay_frames == 0`. `forward` yields only
complete frames; nothing is dropped.

Muffling is decided per caller utterance (a rising edge of the speech mask) with
the seeded `Muffle` Bernoulli; a muffled utterance passes through the low-pass,
others through an identity line with the same hold-back, so alignment never changes.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict

from duplexbench.channel import mulaw
from duplexbench.channel.bursts import Bursts
from duplexbench.channel.gilbert_elliott import GE, conceal
from duplexbench.channel.muffle import Muffle, _lowpass
from duplexbench.channel.noise import NoiseBed
from duplexbench.channel.resample import (_BL_TAPS_PER_KHZ, _G712_HI_HZ, _G712_LO_HZ, Resampler,
                                          _lowpass_taps, _odd, _StreamFIR)

if TYPE_CHECKING:
    from duplexbench.thresholds import Thresholds

CONDITIONS_DIR = Path(__file__).resolve().parents[3] / "configs" / "conditions"
_FRAMES_PER_S = 50
_MS_PER_S = 1e3
Effect = Literal["noise", "bursts", "muffle", "loss", "band_limit"]
_EFFECTS = ("noise", "bursts", "muffle", "loss", "band_limit")


class Condition(BaseModel):
    """One channel condition (spec §5). Values of every parameter come from Thresholds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["C0", "C1", "W"]
    rate: int                       # line rate: 8000 (C0, C1) or 16000 (W)
    codec: bool                     # G.711 μ-law on the line
    noise: bool = False
    noise_snr_db: float = 0.0
    noise_drift_db: float = 0.0
    bursts: bool = False
    burst_per_min: float = 0.0
    burst_len_ms: tuple[float, float] = (0.0, 0.0)
    burst_level_db: float = 0.0
    muffle: bool = False
    muffle_share: float = 0.0
    muffle_cutoff_hz: float = 0.0
    loss: bool = False
    ge_p: float = 0.0
    ge_r: float = 0.0
    ge_loss_in_bad: float = 0.0
    band_limit: bool = False        # G.712 telephone band (C1)
    rms_active_dbfs: float = 0.0    # speech-active mask for the noise reference level
    frame_ms: int = 0

    def __str__(self) -> str:       # the `calls.condition` column
        return self.name


def load_condition(name: str, th: "Thresholds", conditions_dir: Path | None = None) -> Condition:
    """Read `configs/conditions/<name>.yaml` (rate, codec, effects) and fill every
    parameter from `th`."""
    p = Path(conditions_dir or CONDITIONS_DIR) / f"{name}.yaml"
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if raw.get("name", name) != name:
        raise ValueError(f"{p}: name {raw.get('name')!r} != {name!r}")
    effects = list(raw.get("effects") or [])
    bad = [e for e in effects if e not in _EFFECTS]
    if bad:
        raise ValueError(f"{p}: unknown effects {bad}")
    on = set(effects)
    kw: dict = {"name": name, "rate": int(raw["rate"]), "codec": raw["codec"] == "mulaw",
                "rms_active_dbfs": th.rms_active_dbfs, "frame_ms": th.frame_ms,
                "band_limit": "band_limit" in on}
    if "noise" in on:
        kw.update(noise=True, noise_snr_db=th.noise_snr_db, noise_drift_db=th.noise_drift_db)
    if "bursts" in on:
        kw.update(bursts=True, burst_per_min=th.burst_per_min,
                  burst_len_ms=tuple(th.burst_len_ms), burst_level_db=th.burst_level_db)
    if "muffle" in on:
        kw.update(muffle=True, muffle_share=th.muffle_share, muffle_cutoff_hz=th.muffle_cutoff_hz)
    if "loss" in on:
        kw.update(loss=True, ge_p=th.ge_p, ge_r=th.ge_r, ge_loss_in_bad=th.ge_loss_in_bad)
    return Condition(**kw)


def _to_int16(y: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(y), -32768, 32767).astype(np.int16)


class _Hold:
    """Identity path with the same hold-back as a `_StreamFIR` of group delay `d`
    (releases all but the last `d` samples), so a muffled and an unmuffled
    utterance leave the chain with identical alignment."""

    def __init__(self, d: int):
        self.d = d
        self.buf = np.zeros(0)

    def push(self, x: np.ndarray) -> np.ndarray:
        buf = np.concatenate([self.buf, x])
        k = max(0, len(buf) - self.d)
        out, self.buf = buf[:k], buf[k:]
        return out


class ChannelChain:
    """Seeded, streaming, two-direction channel (spec §5).

    Effect seeds are derived from `seed_fwd` (noise +0, bursts +1, muffle +2,
    forward loss +3); the return loss uses `seed_back`. Two different seeds give
    independent loss patterns.
    """

    def __init__(self, condition: Condition, seed_fwd: int, seed_back: int,
                 rate_out: int | None = None):
        c = condition
        self.condition = c
        self.rate = int(rate_out or c.rate)
        self.frame_len = self.rate // _FRAMES_PER_S
        self.seed_fwd, self.seed_back = seed_fwd, seed_back
        self.noise = NoiseBed(seed_fwd, c.noise_snr_db, c.noise_drift_db, self.rate) if c.noise else None
        self.bursts = (Bursts(seed_fwd + 1, c.burst_per_min, c.burst_len_ms, c.burst_level_db, self.rate)
                       if c.bursts else None)
        self.muffle = Muffle(seed_fwd + 2, c.muffle_share, c.muffle_cutoff_hz, self.rate) if c.muffle else None
        self.fwd_ge = GE(seed_fwd + 3, c.ge_p, c.ge_r, c.ge_loss_in_bad) if c.loss else None
        self.back_ge = GE(seed_back, c.ge_p, c.ge_r, c.ge_loss_in_bad) if c.loss else None
        hold = 0
        self._bl = None
        if c.band_limit:
            n = _odd(_BL_TAPS_PER_KHZ * self.rate / _MS_PER_S)
            taps = (_lowpass_taps(_G712_HI_HZ, self.rate, n, "blackman")
                    - _lowpass_taps(_G712_LO_HZ, self.rate, n, "blackman"))
            self._bl = _StreamFIR(taps)
            hold += self._bl.d
        self._mf = self._md = None
        if self.muffle is not None:
            self._mf = _StreamFIR(_lowpass(c.muffle_cutoff_hz, self.rate))
            self._md = _Hold(self._mf.d)
            hold += self._mf.d
        self.hold_samples = hold
        self.delay_frames = -(-hold // self.frame_len)          # ceil
        self._fwd_rs: dict[int, Resampler] = {}
        self._fwd_buf = np.zeros(0, np.int16)
        self._fwd_prev: bytes | None = None
        self._fwd_idx = 0
        self._muffling = False
        self._last_speech = False
        self.muffled_utterances: list[bool] = []
        self._back_rs: dict[int, Resampler] = {}
        self._back_buf = np.zeros(0, np.int16)
        self._back_prev: bytes | None = None
        self.fwd_lost: list[int] = []       # forward frame indices lost
        self.back_lost: list[int] = []
        self._back_idx = 0

    # -- delivery ---------------------------------------------------------------
    def delivery(self, native_mulaw: bool) -> str:
        """What the adapter receives: "mulaw" or "pcm16@<rate>" (spec §5, recorded per adapter)."""
        if self.condition.codec and native_mulaw:
            return "mulaw"
        return f"pcm16@{self.rate}"

    def decode(self, payload: bytes) -> np.ndarray:
        """Line frame bytes -> int16 pcm (μ-law under a codec, raw PCM16 otherwise)."""
        if self.condition.codec:
            return mulaw.decode(payload)
        return np.frombuffer(payload, dtype=np.int16).copy()

    # -- forward ------------------------------------------------------------------
    def forward(self, pcm_src: np.ndarray, rate_src: int, speech_mask) -> Iterator[bytes]:
        """Process a chunk of caller source audio; yield every completed line frame
        (μ-law bytes, or PCM16 bytes when the condition has no codec).

        `speech_mask`: one bool per sample, per 20 ms frame, or a scalar for the chunk.
        """
        x = np.asarray(pcm_src, dtype=np.int16).ravel()
        n = len(x)
        mask = np.asarray(speech_mask, dtype=bool).ravel()
        if mask.size == 1:
            mask = np.full(n, bool(mask[0]))
        elif mask.size != n:
            mask = np.repeat(mask, -(-n // mask.size))[:n]
        if rate_src != self.rate:
            rs = self._fwd_rs.setdefault(rate_src, Resampler(rate_src, self.rate))
            x = rs.push(x)
            m_idx = (np.arange(len(x)) * n // max(1, len(x))).astype(int)
            mask = mask[m_idx] if n else np.zeros(len(x), bool)
        y = x
        if self.noise is not None:
            y = self.noise.apply(y, mask)
        if self.bursts is not None:
            y = self.bursts.apply(y)
        yf = np.asarray(y, dtype=np.float64)
        if self.muffle is not None:
            yf = self._muffle_stream(yf, mask)
        if self._bl is not None:
            yf = self._bl.push(yf)
        out = _to_int16(yf)
        self._fwd_buf = np.concatenate([self._fwd_buf, out])
        while len(self._fwd_buf) >= self.frame_len:
            fr, self._fwd_buf = self._fwd_buf[:self.frame_len], self._fwd_buf[self.frame_len:]
            yield self._line_frame(fr)

    def _muffle_stream(self, x: np.ndarray, mask: np.ndarray) -> np.ndarray:
        parts = []
        i = 0
        while i < len(x):
            # split at rising edges of the mask: a new utterance draws a new decision
            if mask[i] and not self._last_speech:
                self.muffle.apply([np.zeros(0, np.int16)])      # one seeded draw per utterance
                self._muffling = self.muffle.muffled[0]
                self.muffled_utterances.append(self._muffling)
            j = i + 1
            while j < len(x) and not (mask[j] and not mask[j - 1]):
                j += 1
            seg = x[i:j]
            filt, dry = self._mf.push(seg), self._md.push(seg)   # equal lengths
            parts.append(filt if self._muffling else dry)
            self._last_speech = bool(mask[j - 1])
            i = j
        return np.concatenate(parts) if parts else np.zeros(0)

    def _line_frame(self, pcm: np.ndarray) -> bytes:
        payload = mulaw.encode(pcm) if self.condition.codec else pcm.astype(np.int16).tobytes()
        idx = self._fwd_idx
        self._fwd_idx += 1
        if self.fwd_ge is not None and self.fwd_ge.next():
            self.fwd_lost.append(idx)
            payload = (conceal(self._fwd_prev, len(payload)) if self.condition.codec
                       else (self._fwd_prev or bytes(len(payload))))
        self._fwd_prev = payload
        return payload

    # -- return -------------------------------------------------------------------
    def back(self, pcm_provider: np.ndarray, rate: int) -> Iterator[np.ndarray]:
        """Provider audio -> line -> decoded int16 frames for the playout queue."""
        x = np.asarray(pcm_provider, dtype=np.int16).ravel()
        if rate != self.rate:
            rs = self._back_rs.setdefault(rate, Resampler(rate, self.rate))
            x = rs.push(x)
        self._back_buf = np.concatenate([self._back_buf, x])
        while len(self._back_buf) >= self.frame_len:
            fr, self._back_buf = self._back_buf[:self.frame_len], self._back_buf[self.frame_len:]
            yield self._back_frame(fr)

    def back_delay_ms(self, rate: int) -> float:
        """Streaming hold-back of the return resampler for provider audio at `rate`."""
        if rate == self.rate:
            return 0.0
        return self._back_rs.setdefault(rate, Resampler(rate, self.rate)).delay_ms

    def _back_frame(self, pcm: np.ndarray) -> np.ndarray:
        idx = self._back_idx
        self._back_idx += 1
        line = mulaw.encode(pcm) if self.condition.codec else pcm.astype(np.int16).tobytes()
        if self.back_ge is not None and self.back_ge.next():
            self.back_lost.append(idx)
            line = (conceal(self._back_prev, len(line)) if self.condition.codec
                    else (self._back_prev or bytes(len(line))))
        self._back_prev = line
        return self.decode(line)
