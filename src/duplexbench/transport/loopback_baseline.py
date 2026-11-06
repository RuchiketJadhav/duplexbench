"""Loopback baseline: caller leg -> harness echo endpoint -> caller leg (spec §6.2; BUILD_PLAN M9-T02).

    run_loopback(clock, trunk, chain, th, probes=20, probe_every=25) -> dict

The caller leg sends 20 ms frames through the channel chain's forward path
(`ChannelChain.forward`: the same μ-law line frames a phone call carries). Every
`probe_every`-th frame is a probe tone, the rest silence. A harness echo endpoint on
the agent leg returns every frame it receives. The caller leg decodes what comes back
(`ChannelChain.decode`) and detects a probe as the first received frame whose RMS is
>= `th.rms_active_dbfs` after a run of inactive frames (spec §1.2 activity rule on
played frames, never VAD). A probe's echo is the first onset within one probe
spacing (probe_every x frame_ms) after its send; none = lost. This assumes the round
trip is shorter than the spacing (500 ms at the defaults).

Result ("network baseline" numbers for the run card, `run_card_fill.fill(loopback=...)`):

    {"rtt_ms_median", "rtt_ms": [...], "jitter_ms_median" (median |successive RTT diff|),
     "n_probes", "n_received", "lost", "trunk", "condition", "frame_ms"}

Phone-path latencies are shown raw and minus `rtt_ms_median` (phone_path.with_baseline).
"""
from __future__ import annotations

import asyncio
import math
from statistics import median
from typing import Any

import numpy as np

from duplexbench.transport.phone_path import TrunkClient, caller_leg

NS_PER_MS = 1_000_000
PROBE_HZ = 1000.0          # probe tone frequency (inside the G.712 band)
PROBE_AMPL = 8000          # int16 peak of the probe tone (about -12 dBFS)
FULL_SCALE = 32768.0


def _dbfs(pcm: np.ndarray) -> float:
    x = pcm.astype(np.float64)
    rms = math.sqrt(float(np.mean(x * x))) if len(x) else 0.0
    return -math.inf if rms <= 0 else 20 * math.log10(rms / FULL_SCALE)


def probe_frames(chain: Any, n_frames: int, probe_every: int) -> tuple[list[bytes], list[int]]:
    """Line frames for the whole probe train and the frame indices that carry a probe."""
    rate, flen = chain.rate, chain.frame_len
    t = np.arange(flen) / rate
    tone = (PROBE_AMPL * np.sin(2 * np.pi * PROBE_HZ * t)).astype(np.int16)
    silence = np.zeros(flen, np.int16)
    src, idx = [], []
    for k in range(n_frames):
        on = k % probe_every == 0
        src.append(tone if on else silence)
        if on:
            idx.append(k)
    pcm = np.concatenate(src)
    mask = np.concatenate([np.full(flen, k in idx) for k in range(n_frames)])
    frames = list(chain.forward(pcm, rate, mask))
    # hold-back of the chain's streaming filters: flush with silence so every frame leaves
    while len(frames) < n_frames:
        frames += list(chain.forward(silence, rate, False))
    return frames[:n_frames], idx


async def echo_endpoint(trunk: TrunkClient) -> int:
    """Return every media frame received on the agent leg; ends on hangup."""
    n = 0
    while True:
        ev = await trunk.recv("agent")
        if ev.kind == "hangup":
            return n
        if ev.kind == "media":
            await trunk.send("agent", ev.payload)
            n += 1


async def run_loopback(clock: Any, trunk: TrunkClient, chain: Any, th: Any, *,
                       probes: int = 20, probe_every: int = 25) -> dict:
    """Measure trunk round trip and jitter with `probes` tone probes (see module doc)."""
    n_frames = probes * probe_every
    frames, idx = probe_frames(chain, n_frames, probe_every)
    await trunk.connect()
    echo = clock.task(echo_endpoint(trunk))
    tail_ms = probe_every * th.frame_ms * 4
    try:
        sends, ear = await caller_leg(clock, trunk, frames, th.frame_ms, tail_ms=tail_ms)
    finally:
        await trunk.close()
        await clock.idle(asyncio.gather(echo, return_exceptions=True))
    # onsets of received probes: active frame after an inactive one
    onsets: list[int] = []
    prev = False
    for fr in ear:
        on = _dbfs(chain.decode(fr.payload)) >= th.rms_active_dbfs
        if on and not prev:
            onsets.append(fr.t_ns)
        prev = on
    send_t = [sends[k] for k in idx]
    window = probe_every * th.frame_ms * NS_PER_MS   # assumes rtt < probe spacing
    rtts: list[float] = []
    for ts in send_t:
        hit = next((o for o in onsets if ts <= o < ts + window), None)
        if hit is not None:
            rtts.append((hit - ts) / NS_PER_MS)
    jit = [abs(b - a) for a, b in zip(rtts, rtts[1:])]
    return {
        "trunk": getattr(trunk, "name", type(trunk).__name__),
        "condition": str(getattr(chain, "condition", "")),
        "frame_ms": th.frame_ms,
        "n_probes": len(send_t), "n_received": len(rtts), "lost": len(send_t) - len(rtts),
        "rtt_ms": rtts,
        "rtt_ms_median": median(rtts) if rtts else None,
        "jitter_ms_median": median(jit) if jit else (0.0 if rtts else None),
    }
