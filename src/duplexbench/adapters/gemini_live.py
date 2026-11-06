"""Gemini Live adapter (BUILD_PLAN M6-T03; docs/providers.md §4, checked 2026-11-04).

Turn-based S2S over the stateful `BidiGenerateContent` WebSocket. No μ-law: the
adapter receives PCM16 at the line rate (runner delivery `pcm16@<rate>`, because
`caps.native_mulaw=False`), resamples it to 16 kHz with `channel.resample.Resampler`
and sends `audio/pcm;rate=16000`. Output is PCM16 24 kHz; the runner resamples it.

    harness event        <- Gemini server message                              status
    session_open         <- `setupComplete`                                    confirmed (api/live)
    audio                <- `serverContent.modelTurn.parts[].inlineData`        confirmed
    first_audio_byte     <- base class (first audio after the caller turn end)
    cancel_or_truncate   <- `serverContent.interrupted: true`                   confirmed
    output_transcript    <- `serverContent.outputTranscription.text`            confirmed (field: text UNCONFIRMED)
    tool_call            <- `toolCall.functionCalls[]` {id, name, args}         confirmed (inner fields UNCONFIRMED)
    (log only)           <- `toolCallCancellation.ids`, `goAway`, `turnComplete`, `generationComplete`
    usage                <- `usageMetadata`                                     confirmed
    tool result          -> `toolResponse.functionResponses[]` {id, name, response}

There is no client truncate event (providers.md §4).
"""
from __future__ import annotations

import base64
import os
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import numpy as np

from duplexbench.adapters._convert import InputConverter, parse_json
from duplexbench.adapters.ws_adapter import WsAdapter
from duplexbench.transport.live_stream import Connector
from duplexbench.adapters.base import AdapterCaps, AdapterEvent

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.context import CallContext

# UNCONFIRMED per docs/providers.md: host (only the path is on the fetched pages)
DEFAULT_ENDPOINT = ("wss://generativelanguage.googleapis.com/ws/"
                    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
INPUT_RATE = 16000          # "raw 16-bit PCM audio, 16kHz, little-endian" [Fact]
OUTPUT_RATE = 24000         # "raw 16-bit PCM audio, 24kHz" [Fact]


def function_declarations(tool_schemas: list[dict]) -> list[dict]:
    return [{"name": t["name"], "description": t.get("description", ""),
             "parameters": t.get("parameters") or {"type": "object", "properties": {}}}
            for t in tool_schemas]


def _rate_of(mime: str, default: int) -> int:
    for part in mime.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "rate" and v.isdigit():
            return int(v)
    return default


class GeminiLive(WsAdapter):
    name = "gemini_live"
    caps = AdapterCaps(cancel_event=True, native_mulaw=False, output_transcript=True,
                       usage_fields=True)
    components_only = False
    caps_note = "no μ-law: PCM16 16 kHz in, PCM16 24 kHz out (providers.md §4)"

    def __init__(self, clock: "Clock", config: dict, *, connector: Connector | None = None,
                 tool_schemas: list[dict] | None = None, api_key: str | None = None):
        super().__init__(clock, config, connector)
        self.model = str(self.config.get("model") or "gemini-3.8-live")
        self.tool_schemas = list(tool_schemas or [])
        env = self.acfg.get("api_key_env", "GEMINI_API_KEY")
        self.api_key = api_key if api_key is not None else os.environ.get(env, "")
        self.in_rate = int(self.acfg.get("input_rate", INPUT_RATE))
        self.conv: InputConverter | None = None
        self._open_ev: AdapterEvent | None = None
        self.tool_names: dict[str, str] = {}

    def setup_message(self) -> dict:
        gen: dict[str, Any] = {"responseModalities": ["AUDIO"]}
        if self.acfg.get("voice"):
            gen["speechConfig"] = {"voiceConfig": {"prebuiltVoiceConfig": {
                "voiceName": self.acfg["voice"]}}}
        setup: dict[str, Any] = {"model": f"models/{self.model}", "generationConfig": gen,
                                 "outputAudioTranscription": {}}
        if self.acfg.get("input_transcription", True):
            setup["inputAudioTranscription"] = {}
        if self.acfg.get("instructions"):
            setup["systemInstruction"] = {"parts": [{"text": self.acfg["instructions"]}]}
        td = self.config.get("turn_detection")
        if isinstance(td, dict) and td:
            setup["realtimeInputConfig"] = dict(td)
        if self.tool_schemas:
            setup["tools"] = [{"functionDeclarations": function_declarations(self.tool_schemas)}]
        return {"setup": setup}

    async def open(self, call: "CallContext") -> None:
        self.call = call
        self.conv = InputConverter(call, native_mulaw=False)
        url = str(self.acfg.get("endpoint") or DEFAULT_ENDPOINT)
        if self.api_key:
            url += ("&" if "?" in url else "?") + f"key={self.api_key}"   # UNCONFIRMED per docs/providers.md: key in query vs x-goog-api-key header
        await self._connect(url)
        await self._send_json(self.setup_message())

    async def send_frame(self, mulaw_bytes: bytes, t_ns: int) -> None:
        if self.conv is None:
            return
        pcm = self.conv.to_pcm(mulaw_bytes, self.in_rate)
        if pcm.size == 0:
            return
        data = base64.b64encode(pcm.astype("<i2").tobytes()).decode("ascii")
        # UNCONFIRMED per docs/providers.md: `realtimeInput.audio` (vs legacy `mediaChunks[]`)
        await self._send_json({"realtimeInput": {"audio": {
            "data": data, "mimeType": f"audio/pcm;rate={self.in_rate}"}}})

    async def tool_result(self, tool_call_id: str, result: dict) -> None:
        await self._send_json({"toolResponse": {"functionResponses": [{
            "id": tool_call_id, "name": self.tool_names.get(tool_call_id, ""),
            "response": result}]}})

    def _map(self, msg: str | bytes, t_ns: int) -> Iterable[AdapterEvent]:
        obj = parse_json(msg)
        if obj is None:
            self.raw_log.append((t_ns, "non-json"))
            return
        if "setupComplete" in obj:
            yield AdapterEvent("session_open", t_ns, "setupComplete", {})
        sc = obj.get("serverContent")
        if isinstance(sc, dict):
            for part in (sc.get("modelTurn") or {}).get("parts") or []:
                inl = part.get("inlineData") or {}
                mime = str(inl.get("mimeType", ""))
                if inl.get("data") and mime.startswith("audio/pcm"):
                    pcm = np.frombuffer(base64.b64decode(inl["data"]), dtype="<i2").astype(np.int16)
                    yield AdapterEvent("audio", t_ns, "serverContent.modelTurn",
                                       {"pcm": pcm, "rate": _rate_of(mime, OUTPUT_RATE)})
            if sc.get("interrupted"):
                yield AdapterEvent("cancel_or_truncate", t_ns, "serverContent.interrupted", {})
            ot = sc.get("outputTranscription")
            if isinstance(ot, dict) and ot.get("text"):
                yield AdapterEvent("output_transcript", t_ns, "serverContent.outputTranscription",
                                   {"text": ot["text"], "final": False})
            for k in ("turnComplete", "generationComplete"):
                if sc.get(k):
                    self.raw_log.append((t_ns, f"serverContent.{k}"))
        tc = obj.get("toolCall")
        if isinstance(tc, dict):
            for fc in tc.get("functionCalls") or []:
                fid = str(fc.get("id", ""))
                self.tool_names[fid] = str(fc.get("name", ""))
                yield AdapterEvent("tool_call", t_ns, "toolCall",
                                   {"id": fid, "name": fc.get("name"),
                                    "arguments": fc.get("args") or {}})
        if "toolCallCancellation" in obj:
            self.raw_log.append((t_ns, "toolCallCancellation"))
        if "goAway" in obj:
            self.raw_log.append((t_ns, "goAway"))
        um = obj.get("usageMetadata")
        if isinstance(um, dict):
            yield AdapterEvent("usage", t_ns, "usageMetadata", dict(um))
        if "error" in obj:
            err = obj["error"] if isinstance(obj["error"], dict) else {"message": str(obj["error"])}
            yield AdapterEvent("error", t_ns, "error",
                               {"type": str(err.get("status", "provider_error")),
                                "message": str(err.get("message", ""))})
