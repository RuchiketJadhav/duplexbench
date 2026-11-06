"""Shared helpers for the M6 live adapters (BUILD_PLAN §2.3; was adapters/_ws.py).

* `InputConverter` turns what `send_frame` receives (μ-law 8 kHz, or PCM16 at the
  line rate when the condition has no codec or the adapter is not native μ-law;
  mirror of `ChannelChain.delivery`) into what a provider wants.
* `system_dict(config)` accepts a `systems.SystemConfig` or a plain mapping and
  returns the mapping; adapters read their extras from its `params:` block
  (systems.py schema; the M6 `adapter_config:` key was folded into `params:`).
* `parse_json` / `dumps`: tolerant JSON in, compact JSON out.

The socket itself is `transport/live_stream.LiveStream` (adapters/ws_adapter.py).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import numpy as np

from duplexbench.channel import mulaw
from duplexbench.channel.resample import Resampler

if TYPE_CHECKING:
    from duplexbench.context import CallContext

MULAW_RATE = 8000           # G.711: always 8 kHz (Twilio docs; docs/providers.md §B)


def system_dict(config: Any) -> dict:
    """SystemConfig | mapping -> plain dict (the system YAML shape)."""
    if config is None:
        return {}
    dump = getattr(config, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json"))
    return dict(config)


def params_of(config: Any) -> dict:
    """The adapter-specific `params:` block of a system config."""
    return dict(system_dict(config).get("params") or {})


class InputConverter:
    """Caller frames as delivered by the runner -> μ-law 8 kHz or PCM16 at a target rate."""

    def __init__(self, call: "CallContext | None", native_mulaw: bool):
        cond = getattr(call, "condition", None)
        codec = bool(getattr(cond, "codec", True))
        self.line_rate = int(getattr(cond, "rate", MULAW_RATE))
        self.mulaw_in = codec and native_mulaw          # == ChannelChain.delivery(...) == "mulaw"
        self.codec_line = codec
        self._rs: dict[int, Resampler] = {}

    def pcm(self, frame: bytes) -> np.ndarray:
        """Delivered frame -> int16 PCM at the line rate."""
        if self.mulaw_in:
            return mulaw.decode(frame)
        return np.frombuffer(frame, dtype=np.int16).copy()

    def _resample(self, pcm: np.ndarray, rate: int) -> np.ndarray:
        if rate == self.line_rate:
            return pcm
        rs = self._rs.get(rate)
        if rs is None:
            rs = self._rs[rate] = Resampler(self.line_rate, rate)
        return rs.push(pcm)

    def to_mulaw8k(self, frame: bytes) -> bytes:
        if self.mulaw_in:
            return bytes(frame)
        return mulaw.encode(self._resample(self.pcm(frame), MULAW_RATE))

    def to_pcm(self, frame: bytes, rate: int) -> np.ndarray:
        return self._resample(self.pcm(frame), rate)


def parse_json(msg: str | bytes) -> dict | None:
    try:
        obj = json.loads(msg)
    except (ValueError, UnicodeDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))
