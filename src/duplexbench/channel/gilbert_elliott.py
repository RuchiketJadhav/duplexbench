"""Seeded Gilbert-Elliott frame loss and concealment (spec §5; BUILD_PLAN M1-T06).

Two states, Good and Bad. Per frame: emit (lost with probability `loss_in_bad`
in Bad, never in Good), then transition G->B with probability p, B->G with r.
Starts in Good. Long-run loss = loss_in_bad * p/(p+r); mean Bad sojourn = 1/r
frames. With the C1 defaults p=0.0034, r=0.1667, loss_in_bad=1.0: 2.0 % loss in
bursts averaging 6 frames = 120 ms (spec: 100-150 ms).

Concealment repeats the previous frame (G.711 Appendix I style, spec §5
[Assumption]); the first frame of a stream is concealed with μ-law silence.
"""
from __future__ import annotations

import numpy as np

from duplexbench.channel.mulaw import SILENCE

FRAME_BYTES = 160               # 20 ms of 8 kHz μ-law (BUILD_PLAN §2.2)


class GE:
    def __init__(self, seed: int, p: float, r: float, loss_in_bad: float):
        self.seed, self.p, self.r, self.loss_in_bad = seed, float(p), float(r), float(loss_in_bad)
        self._rng = np.random.default_rng(seed)
        self.bad = False

    def next(self) -> bool:
        """True = this frame is lost."""
        u_loss, u_move = self._rng.random(2)
        lost = self.bad and u_loss < self.loss_in_bad
        self.bad = (u_move >= self.r) if self.bad else (u_move < self.p)
        return bool(lost)


def conceal(prev_frame: bytes | np.ndarray | None, frame_len: int = FRAME_BYTES):
    """Replacement for a lost frame: a copy of the previous frame, or μ-law
    silence of `frame_len` bytes when there is no previous frame."""
    if prev_frame is None:
        return SILENCE * frame_len
    if isinstance(prev_frame, np.ndarray):
        return prev_frame.copy()
    return bytes(prev_frame)
