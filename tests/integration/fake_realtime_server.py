"""Fake OpenAI Realtime WebSocket server (subset of docs/providers.md §3) for M3 tests.

Runs on its own event loop in a daemon thread, so it can serve a `cli run` that
owns the main loop. No network beyond 127.0.0.1; no API key is checked beyond
being present.

Protocol subset:
    session.update                 -> session.created, session.updated (session echoed)
    input_audio_buffer.append      -> energy VAD on the μ-law audio:
                                      speech onset   -> input_audio_buffer.speech_started
                                                        (+ response.done{cancelled, turn_detected}
                                                        when a response is streaming)
                                      silence_ms of quiet after speech -> speech_stopped,
                                                        committed, then a response
    response                       -> response.created, response.output_audio.delta (μ-law tone,
                                      chunk_ms per message, paced at real time), transcript
                                      delta/done, response.done{completed, usage}
    conversation.item.truncate     -> conversation.item.truncated
    conversation.item.create{function_call_output} + response.create -> an audio response

Scripted behaviours, one dict per connection in order (last one repeats):
    {"drop_at_s": 1.0}           abort the TCP transport 1.0 s after session.update (no close frame)
    {"function_call": {"name": "get_account", "arguments": {...}}}
                                 first turn answers with function_call_arguments.done instead of audio
    {"reply_ms": 1500}           length of each audio reply
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import threading
import time
from typing import Any

import numpy as np

from duplexbench import _loop
from duplexbench.channel import mulaw

RATE = 8000
VAD_DBFS = -45.0
USAGE = {"total_tokens": 300, "input_tokens": 120, "output_tokens": 180,
         "input_token_details": {"audio_tokens": 100, "text_tokens": 20, "cached_tokens": 0},
         "output_token_details": {"audio_tokens": 170, "text_tokens": 10}}


def _tone(n: int, start: int, hz: float = 440.0, dbfs: float = -20.0) -> np.ndarray:
    amp = 32768 * 10 ** (dbfs / 20) * np.sqrt(2)
    t = (np.arange(n) + start) / RATE
    return np.rint(amp * np.sin(2 * np.pi * hz * t)).astype(np.int16)


class _Conn:
    def __init__(self, server: "FakeRealtimeServer", ws: Any, idx: int, behaviour: dict):
        self.server, self.ws, self.idx, self.b = server, ws, idx, behaviour
        self.ids = itertools.count(1)
        self.speaking = False
        self.quiet_ms = 0
        self.audio_ms = 0
        self.turns = 0
        self.resp_task: asyncio.Task | None = None
        self.resp_id: str | None = None
        self.fc_done = False
        self.received: list[dict] = []
        self.appends = 0

    def nid(self, p: str) -> str:
        return f"{p}_{self.idx}_{next(self.ids)}"

    async def send(self, obj: dict) -> None:
        obj.setdefault("event_id", self.nid("ev"))
        await self.ws.send(json.dumps(obj))

    async def run(self) -> None:
        async for raw in self.ws:
            e = json.loads(raw)
            typ = e.get("type")
            if typ == "input_audio_buffer.append":
                self.appends += 1
                await self.on_audio(base64.b64decode(e["audio"]))
                continue
            self.received.append(e)
            if typ == "session.update":
                await self.send({"type": "session.created", "session": {"id": self.nid("sess")}})
                await self.send({"type": "session.updated",
                                 "session": dict(e.get("session") or {}, id=self.nid("sess"))})
                drop = self.b.get("drop_at_s")
                if drop is not None:
                    asyncio.get_running_loop().call_later(drop, self.abort)
            elif typ == "conversation.item.truncate":
                await self.send({"type": "conversation.item.truncated", "item_id": e.get("item_id"),
                                 "content_index": e.get("content_index", 0),
                                 "audio_end_ms": e.get("audio_end_ms")})
            elif typ == "response.create":
                self.start_response(audio=True)

    def abort(self) -> None:
        self.server.drops += 1
        self.ws.transport.abort()

    async def on_audio(self, b: bytes) -> None:
        pcm = mulaw.decode(b).astype(np.float64)
        ms = len(pcm) * 1000 // RATE
        self.audio_ms += ms
        rms = np.sqrt(np.mean(pcm * pcm)) if len(pcm) else 0.0
        loud = rms > 0 and 20 * np.log10(rms / 32768) > VAD_DBFS
        if loud:
            self.quiet_ms = 0
            if not self.speaking:
                self.speaking = True
                await self.send({"type": "input_audio_buffer.speech_started",
                                 "audio_start_ms": self.audio_ms, "item_id": self.nid("item")})
                if self.resp_task is not None and not self.resp_task.done():
                    self.resp_task.cancel()
                    await self.send({"type": "response.done", "response": {
                        "id": self.resp_id, "status": "cancelled",
                        "status_details": {"reason": "turn_detected"}, "usage": USAGE}})
        elif self.speaking:
            self.quiet_ms += ms
            if self.quiet_ms >= self.b.get("silence_ms", 300):
                self.speaking = False
                await self.send({"type": "input_audio_buffer.speech_stopped",
                                 "audio_end_ms": self.audio_ms, "item_id": self.nid("item")})
                await self.send({"type": "input_audio_buffer.committed", "item_id": self.nid("item")})
                self.turns += 1
                fc = self.b.get("function_call")
                self.start_response(audio=not (fc and not self.fc_done))

    def start_response(self, audio: bool) -> None:
        self.resp_id = self.nid("resp")
        self.resp_task = asyncio.get_running_loop().create_task(self.respond(self.resp_id, audio))

    async def respond(self, rid: str, audio: bool) -> None:
        await self.send({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
        if not audio:
            fc = self.b["function_call"]
            self.fc_done = True
            await self.send({"type": "response.function_call_arguments.done", "response_id": rid,
                             "item_id": self.nid("fc"), "output_index": 0,
                             "call_id": self.nid("call"), "name": fc["name"],
                             "arguments": json.dumps(fc.get("arguments") or {})})
            await self.send({"type": "response.done", "response": {"id": rid, "status": "completed",
                                                                   "usage": USAGE}})
            return
        item = self.nid("item")
        chunk_ms = self.b.get("chunk_ms", 100)
        n_chunk = RATE * chunk_ms // 1000
        total = RATE * self.b.get("reply_ms", 1500) // 1000
        t0 = time.perf_counter()
        pos = 0
        while pos < total:
            n = min(n_chunk, total - pos)
            await self.send({"type": "response.output_audio.delta", "response_id": rid,
                             "item_id": item, "output_index": 0, "content_index": 0,
                             "delta": base64.b64encode(mulaw.encode(_tone(n, pos))).decode()})
            pos += n
            await self.send({"type": "response.output_audio_transcript.delta", "response_id": rid,
                             "item_id": item, "delta": "ok "})
            await asyncio.sleep(max(0.0, t0 + pos / RATE - time.perf_counter()))
        await self.send({"type": "response.output_audio_transcript.done", "response_id": rid,
                         "item_id": item, "transcript": "ok " * -(-total // n_chunk)})
        await self.send({"type": "response.done", "response": {"id": rid, "status": "completed",
                                                               "usage": USAGE}})


class FakeRealtimeServer:
    def __init__(self, behaviours: list[dict] | None = None):
        self.behaviours = behaviours or [{}]
        self.conns: list[_Conn] = []
        self.drops = 0
        self.headers: list[dict] = []
        self.paths: list[str] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/realtime"

    async def _handler(self, ws) -> None:
        req = ws.request
        self.headers.append(dict(req.headers) if req is not None else {})
        self.paths.append(req.path if req is not None else "")
        i = len(self.conns)
        c = _Conn(self, ws, i, self.behaviours[min(i, len(self.behaviours) - 1)])
        self.conns.append(c)
        try:
            await c.run()
        except Exception:  # noqa: BLE001 - client went away
            pass
        finally:
            if c.resp_task is not None:
                c.resp_task.cancel()

    def start(self) -> "FakeRealtimeServer":
        ready = threading.Event()

        def main() -> None:
            from websockets.asyncio.server import serve
            loop = _loop.new_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)

            async def boot() -> None:
                self._server = await serve(self._handler, "127.0.0.1", 0, max_size=None,
                                           ping_interval=None, compression=None)
                self.port = next(iter(self._server.sockets)).getsockname()[1]
                ready.set()

            loop.run_until_complete(boot())
            loop.run_forever()
            loop.close()

        self._thread = threading.Thread(target=main, name="fake-realtime", daemon=True)
        self._thread.start()
        if not ready.wait(10):
            raise RuntimeError("fake realtime server did not start")
        return self

    def stop(self) -> None:
        if self._loop is None:
            return

        async def down() -> None:
            self._server.close()
            await self._server.wait_closed()

        fut = asyncio.run_coroutine_threadsafe(down(), self._loop)
        try:
            fut.result(5)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(5)

    def __enter__(self) -> "FakeRealtimeServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
