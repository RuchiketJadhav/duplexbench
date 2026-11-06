"""G.711 μ-law codec in numpy (spec §5; BUILD_PLAN M1-T03).

Bias 0x84, 8 segments, clip at 32635, output bits inverted (so silence is 0xFF).
No `audioop` (removed in Python 3.13).
"""
from __future__ import annotations

import numpy as np

SILENCE = b"\xff"

_BIAS = 0x84
_CLIP = 32635


def encode(pcm: np.ndarray) -> bytes:
    """int16 PCM -> μ-law bytes, one byte per sample."""
    x = np.asarray(pcm).astype(np.int32).ravel()
    sign = np.where(x < 0, 0x80, 0).astype(np.int32)
    mag = np.minimum(np.abs(x), _CLIP) + _BIAS                  # 0x84 .. 0x7FFF
    exp = (np.floor(np.log2(mag)).astype(np.int32) - 7).clip(0, 7)
    mant = (mag >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mant) & 0xFF).astype(np.uint8).tobytes()


def decode(b: bytes) -> np.ndarray:
    """μ-law bytes -> int16 PCM."""
    u = (~np.frombuffer(bytes(b), dtype=np.uint8)).astype(np.int32) & 0xFF
    sign = u & 0x80
    exp = (u >> 4) & 0x07
    mant = u & 0x0F
    mag = (((mant << 3) + _BIAS) << exp) - _BIAS
    return np.where(sign != 0, -mag, mag).astype(np.int16)
