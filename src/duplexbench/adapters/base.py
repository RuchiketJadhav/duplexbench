"""Adapter contract (BUILD_PLAN §2.3; spec §2.1 "what each adapter must record", §1.5 anchors).

Subclasses implement `open`, `send_frame`, `events`, `tool_result`, `close`.
Consumers (the runner) never iterate `events()` directly; they iterate
`adapter.safe_events(call)`, which adds the two behaviours of the base class:

* `first_audio_byte` (spec §1.5 "first agent byte received") is emitted by the
  base class, immediately before the first `audio` event observed at or after
  each caller turn end (`call.turn_ended(t_ns)`); adapters never emit it.
* an adapter never raises out of the stream: any exception inside `events()`
  becomes an `error` event followed by `closed`, and a stream that ends
  without `closed` gets one appended. Exactly one `closed` is yielded, last.

Audio events carry PCM at the provider's rate; the runner resamples.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from duplexbench.context import CallContext

EventKind = Literal["session_open", "first_audio_byte", "audio", "cancel_or_truncate",
                    "output_transcript", "tool_call", "usage", "error", "reconnect", "closed"]


@dataclass
class AdapterEvent:
    kind: EventKind
    t_ns: int                 # clock.now_ns() when the harness observed it
    provider_event: str       # raw provider event name, "" for harness-generated
    payload: dict[str, Any] = field(default_factory=dict)
    # audio: {"pcm": np.ndarray int16, "rate": int}; tool_call: {"id","name","arguments"};
    # usage: {...raw...}; error: {"type", "message"}


@dataclass(frozen=True)
class AdapterCaps:
    cancel_event: bool        # provider emits a cancel/truncate the harness can flush on
    native_mulaw: bool        # accepts μ-law 8 kHz bytes without transcoding
    output_transcript: bool
    usage_fields: bool


class Adapter(ABC):
    name: str
    caps: AdapterCaps
    config: dict              # hashed to config_id (storage.config_id)
    # True when the system under test calls the harness tools over HTTP (Dograh's http_api
    # tools POST to `params.harness_tool_base`): `cli run` then serves the ToolServer app
    # with uvicorn for the whole run (tools/http.py, fix-44). Hosted S2S adapters get tools
    # in-process (ToolClient) and leave it False.
    needs_http_tools: bool = False

    @abstractmethod
    async def open(self, call: "CallContext") -> None: ...

    @abstractmethod
    async def send_frame(self, mulaw: bytes, t_ns: int) -> None: ...

    @abstractmethod
    def events(self) -> AsyncIterator[AdapterEvent]: ...

    @abstractmethod
    async def tool_result(self, tool_call_id: str, result: dict) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @classmethod
    def preflight(cls, config: dict) -> list[str]:
        """Plan-time check of a system config, before any call or spend.

        Returns one human-readable line per missing precondition (env var unset,
        required param absent); [] means nothing is missing. Must not touch the
        network. Default: nothing to check."""
        return []

    # -- base-class behaviour -------------------------------------------------
    async def safe_events(self, call: "CallContext") -> AsyncIterator[AdapterEvent]:
        """`events()` plus `first_audio_byte` emission and error -> closed wrapping."""
        turns_seen = 0          # caller turn ends already answered by a first_audio_byte
        closed = False
        try:
            async for ev in self.events():
                if ev.kind == "first_audio_byte":
                    continue    # harness-generated only (BUILD_PLAN §2.3)
                if ev.kind == "audio" and len(call.turn_end_ns) > turns_seen:
                    last = call.turn_end_ns[-1]
                    if ev.t_ns >= last:
                        turns_seen = len(call.turn_end_ns)
                        yield AdapterEvent("first_audio_byte", ev.t_ns, "",
                                           {"turn_end_ns": last})
                if ev.kind == "closed":
                    closed = True
                    yield ev
                    return
                yield ev
        except Exception as e:  # noqa: BLE001 - the contract: never raise out of the stream
            yield AdapterEvent("error", call.clock.now_ns(), "",
                               {"type": type(e).__name__, "message": str(e)})
        if not closed:
            yield AdapterEvent("closed", call.clock.now_ns(), "", {})
