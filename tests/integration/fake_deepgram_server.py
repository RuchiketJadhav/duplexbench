"""Fake Deepgram streaming server (subset of docs/providers.md §8) for the refasr-deepgram tests.

`websockets.serve` on 127.0.0.1 (random port) on its own event loop in a daemon thread,
like fake_realtime_server.py. No network beyond 127.0.0.1.

Per connection it records the request path, query and headers, and rejects with HTTP 401
(process_request) when the Authorization header is not `Token <key>`. Binary frames are
counted as audio. Scripted replies, by bytes of audio received (16 000 bytes = 1 s of 8 kHz
linear16):

    after `interim_at` bytes  -> Results is_final=false with one word (never released)
    after `final_at` bytes    -> Results is_final=true with FINAL_WORDS, metadata.model_uuid
                                 / model_info
    on {"type":"Finalize"}    -> Results is_final=true, from_finalize=true, FINALIZE_WORDS
    on {"type":"CloseStream"} -> Metadata, then close 1000

Behaviour switches: `uuid` (model_uuid reported), `silent_finish` (ignore Finalize and
CloseStream, never close: client times out), `non_json` (send a non-JSON text frame on the
first audio), `close_code` (close with this code on the first audio).

Chunked script mode (fix-24; off unless `script` is given, so the replies above are
unchanged by default). `script` is a list of {word, start, end, confidence} dicts (s).
The words not yet sent in a final form the current chunk, in order. Audio ms received =
bytes / 16. Every `chunk_ms` of audio -> Results is_final=true with the chunk's words
whose end <= audio ms so far (a later word waits for the next chunk). With `interim_ms`
set, every `interim_ms` of audio -> Results is_final=false with the chunk's words whose
end <= audio ms so far (cumulative, as Deepgram's interims), the first time a word
appears with its text replaced by `misheard[word]` when given (a later interim corrects
it). Finalize -> one from_finalize final (is_final=true, from_finalize=true) with the
words buffered so far: every word not yet in a final whose end <= audio ms received
(fix-40; before, every word left). CloseStream first sends one more final with any
words still left (a script word ending past the audio), so finish() still returns every
script word as before. Finalize may come any number of times mid-stream; the stream stays
open. Without `interim_ms` no interim is sent, so the first words of a chunk wait for
the chunk final (the behaviour measured live on 2026-11-07). `chunk_ms=None`: no chunk
finals at all, finals come only on Finalize (fix-40). `delay_ms` (default 0): every
script-mode Results is sent that many ms after the audio or Finalize that triggered it,
in order (a network delay; 0 sends inline as before).

`silent_after` (fix-41; default None = off): after this many server messages on a
connection the server goes silent: every later message (Results, Metadata) is dropped,
Finalize gets no reply and CloseStream no Metadata and no close, so the client's finish()
times out after seeing exactly `silent_after` messages (0: a stream that never answers).
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit

from duplexbench import _loop

KEY = "test-dg-key-123"
UUID = "uuid-nova3-aaaa"
MODEL_INFO = {"name": "general-nova-3", "version": "2026-01-01.0", "arch": "nova-3"}
INTERIM_WORD = {"word": "helo", "start": 0.1, "end": 0.3, "confidence": 0.4}
FINAL_WORDS = [{"word": "hello", "start": 0.1, "end": 0.35, "confidence": 0.98,
                "punctuated_word": "Hello,"},
               {"word": "there", "start": 0.4, "end": 0.62, "confidence": 0.91,
                "punctuated_word": "there."}]
FINALIZE_WORDS = [{"word": "order", "start": 1.2, "end": 1.5, "confidence": 0.88,
                   "punctuated_word": "Order"}]


def results(words: list[dict], *, is_final: bool, uuid: str, from_finalize: bool = False) -> dict:
    return {"type": "Results", "is_final": is_final, "speech_final": is_final,
            "from_finalize": from_finalize, "start": 0.0, "duration": 1.0,
            "channel": {"alternatives": [{"transcript": " ".join(w["word"] for w in words),
                                          "confidence": 0.9, "words": words}]},
            "metadata": {"request_id": "req-1", "model_uuid": uuid, "model_info": MODEL_INFO}}


class FakeDeepgramServer:
    def __init__(self, *, key: str = KEY, uuid: str = UUID, interim_at: int = 3_200,
                 final_at: int = 9_600, silent_finish: bool = False, non_json: bool = False,
                 close_code: int | None = None, script: list[dict] | None = None,
                 chunk_ms: int | None = 3_000, interim_ms: int | None = None,
                 misheard: dict[str, str] | None = None, delay_ms: int = 0,
                 silent_after: int | None = None) -> None:
        self.key, self.uuid = key, uuid
        self.script = list(script) if script is not None else None
        self.chunk_ms, self.interim_ms = chunk_ms, interim_ms
        self.delay_ms = int(delay_ms)
        self.misheard = dict(misheard or {})
        self.sent: list[list[dict]] = []          # script mode: every Results sent, per connection
        self.interim_at, self.final_at = interim_at, final_at
        self.silent_finish, self.non_json, self.close_code = silent_finish, non_json, close_code
        self.silent_after = silent_after
        self.n_sent: list[int] = []               # server messages sent, per connection
        self.headers: list[dict] = []
        self.paths: list[str] = []
        self.queries: list[dict[str, list[str]]] = []
        self.audio_bytes: list[int] = []
        self.binary_frames: list[int] = []
        self.texts: list[list[dict]] = []
        self.rejected = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self.port: int | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/listen"

    def _process_request(self, connection: Any, request: Any) -> Any:
        from http import HTTPStatus
        if request.headers.get("Authorization") != f"Token {self.key}":
            self.rejected += 1
            return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid credentials\n")
        return None

    def _mute(self, i: int) -> bool:
        return self.silent_after is not None and self.n_sent[i] >= self.silent_after

    async def _send(self, ws: Any, i: int, text: str) -> bool:
        """Send one server message unless the connection went silent; True when sent."""
        if self._mute(i):
            return False
        self.n_sent[i] += 1
        await ws.send(text)
        return True

    async def _handler(self, ws: Any) -> None:
        req = ws.request
        self.headers.append(dict(req.headers))
        self.paths.append(req.path)
        self.queries.append(parse_qs(urlsplit(req.path).query))
        i = len(self.audio_bytes)
        self.audio_bytes.append(0)
        self.binary_frames.append(0)
        self.texts.append([])
        self.n_sent.append(0)
        sent_interim = sent_final = False
        if self.script is not None:
            self.sent.append([])
            await self._scripted(ws, i)
            return
        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    self.audio_bytes[i] += len(raw)
                    self.binary_frames[i] += 1
                    if self.non_json:
                        await ws.send("this is not json")
                        continue
                    if self.close_code is not None:
                        await ws.close(self.close_code, "fake failure")
                        return
                    n = self.audio_bytes[i]
                    if not sent_interim and n >= self.interim_at:
                        sent_interim = True
                        await self._send(ws, i, json.dumps(results([INTERIM_WORD], is_final=False,
                                                                   uuid=self.uuid)))
                    if not sent_final and n >= self.final_at:
                        sent_final = True
                        await self._send(ws, i, json.dumps(results(FINAL_WORDS, is_final=True,
                                                                   uuid=self.uuid)))
                    continue
                msg = json.loads(raw)
                self.texts[i].append(msg)
                if self.silent_finish or self._mute(i):
                    continue
                if msg.get("type") == "Finalize":
                    await self._send(ws, i, json.dumps(results(FINALIZE_WORDS, is_final=True,
                                                               uuid=self.uuid, from_finalize=True)))
                elif msg.get("type") == "CloseStream":
                    await self._send(ws, i, json.dumps({"type": "Metadata", "request_id": "req-1",
                                                        "duration": self.audio_bytes[i] / 16_000,
                                                        "channels": 1}))
                    await ws.close(1000, "")  # threshold-ok normal close
                    return
        except Exception:  # noqa: BLE001 - client went away
            pass

    async def _scripted(self, ws: Any, i: int) -> None:
        """Chunked script mode (see the module docstring)."""
        words, nxt = self.script, 0               # nxt: first word not yet in a final
        seen: set[int] = set()
        next_final, next_interim = self.chunk_ms, self.interim_ms

        out_q: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        async def sender() -> None:
            while True:
                due, msg = await out_q.get()
                left = due - loop.time()
                if left > 0:
                    await asyncio.sleep(left)
                if await self._send(ws, i, json.dumps(msg)):
                    self.sent[i].append(msg)

        sender_task = loop.create_task(sender()) if self.delay_ms > 0 else None

        async def send(ws_words: list[dict], **kw: Any) -> None:
            msg = results(ws_words, uuid=self.uuid, **kw)
            if sender_task is None:
                if await self._send(ws, i, json.dumps(msg)):
                    self.sent[i].append(msg)
            else:
                out_q.put_nowait((loop.time() + self.delay_ms / 1_000, msg))

        async def drain() -> None:
            if sender_task is not None:
                while not out_q.empty():
                    await asyncio.sleep(0.005)
                await asyncio.sleep(self.delay_ms / 1_000 + 0.01)

        def ready(ms: float) -> list[int]:
            return [k for k in range(nxt, len(words)) if words[k]["end"] * 1_000 <= ms]

        try:
            async for raw in ws:
                if isinstance(raw, bytes):
                    self.audio_bytes[i] += len(raw)
                    self.binary_frames[i] += 1
                    ms = self.audio_bytes[i] / 16
                    if next_interim is not None and ms >= next_interim:
                        next_interim += self.interim_ms
                        ks = ready(ms)
                        if ks:
                            out = []
                            for k in ks:
                                w = dict(words[k])
                                if k not in seen and w["word"] in self.misheard:
                                    w["word"] = self.misheard[w["word"]]
                                seen.add(k)
                                out.append(w)
                            await send(out, is_final=False)
                    if next_final is not None and ms >= next_final:
                        next_final += self.chunk_ms
                        ks = ready(ms)
                        if ks:
                            await send([words[k] for k in ks], is_final=True)
                            nxt = ks[-1] + 1
                    continue
                msg = json.loads(raw)
                self.texts[i].append(msg)
                if msg.get("type") == "Finalize":
                    ks = ready(self.audio_bytes[i] / 16)
                    await send([words[k] for k in ks], is_final=True, from_finalize=True)
                    if ks:
                        nxt = ks[-1] + 1
                elif msg.get("type") == "CloseStream":
                    if self._mute(i):
                        continue                  # silent: no flush, no Metadata, no close
                    if nxt < len(words):          # words past the audio: flushed at close
                        await send(list(words[nxt:]), is_final=True)
                        nxt = len(words)
                    await drain()
                    if self._mute(i):
                        continue
                    await self._send(ws, i, json.dumps({"type": "Metadata", "request_id": "req-1",
                                                        "duration": self.audio_bytes[i] / 16_000,
                                                        "channels": 1}))
                    await ws.close(1000, "")  # threshold-ok normal close
                    return
        except Exception:  # noqa: BLE001 - client went away
            pass
        finally:
            if sender_task is not None:
                sender_task.cancel()

    def start(self) -> "FakeDeepgramServer":
        ready = threading.Event()

        def main() -> None:
            from websockets.asyncio.server import serve
            loop = _loop.new_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)

            async def boot() -> None:
                self._server = await serve(self._handler, "127.0.0.1", 0, max_size=None,
                                           ping_interval=None, compression=None,
                                           process_request=self._process_request)
                self.port = next(iter(self._server.sockets)).getsockname()[1]
                ready.set()

            loop.run_until_complete(boot())
            loop.run_forever()
            loop.close()

        self._thread = threading.Thread(target=main, name="fake-deepgram", daemon=True)
        self._thread.start()
        if not ready.wait(10):
            raise RuntimeError("fake deepgram server did not start")
        return self

    def stop(self) -> None:
        if self._loop is None:
            return

        async def down() -> None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            except Exception:  # noqa: BLE001
                pass

        try:
            asyncio.run_coroutine_threadsafe(down(), self._loop).result(timeout=5)
        except Exception:  # noqa: BLE001
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def __enter__(self) -> "FakeDeepgramServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
