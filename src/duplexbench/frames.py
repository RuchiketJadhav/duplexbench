"""Frame contract (BUILD_PLAN §2.2).

Internal PCM is numpy int16, mono, 8,000 Hz, 160 samples per 20 ms frame
(16,000 Hz / 320 samples under the wideband diagnostic condition). μ-law frames
are `bytes` of length 160. `t_ns` is `clock.now_ns()` at send/play time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np


@dataclass
class Frame:
    t_ns: int
    seq: int
    src: Literal["caller", "agent"]
    pcm: np.ndarray | None
    mulaw: bytes | None
