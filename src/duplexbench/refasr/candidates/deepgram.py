"""Deepgram Nova-3 streaming candidate: the reference ASR on the user's instruction
(decision B11, 2026-11-06), although spec §4.3 preferred a pinned open model. API facts
come only from docs/providers.md §8.

    asr = DeepgramASR()                       # key from $DEEPGRAM_API_KEY
    st = asr.stream()
    words = st.push(pcm_int16_frame)          # words released since the last call
    st = asr.stream(provisional=True)         # also release interim words, final=False
    st.finalize()                             # Finalize mid-stream: settle now, stay open
    words += st.finish()                      # Finalize + CloseStream, every word left
    asr.transcribe(pcm8k)                     # one stream, 20 ms frames, finish

Run config:  simulator: {refasr: {candidate: deepgram, kwargs: {pinned_model_uuid: ...}}}

Transport: `stream()` returns a `DeepgramStreamer` whose push()/finish() are synchronous
and thread-safe (the simulated caller calls them through asyncio.to_thread). The
WebSocket (websockets client) runs on a private daemon thread with its own event loop,
opened lazily on the first push(): a stream that is never pushed opens no connection
and costs nothing. Audio goes out as binary frames: int16 numpy arrays as their
little-endian bytes; `bytes` input is μ-law (1 byte per sample, as FakeStreamer) and is
decoded with channel/mulaw.py first. Query: model, language, encoding=linear16,
sample_rate=8000, channels=1, interim_results=true, endpointing, punctuate=false,
smart_format=false, numerals=false (+ utterance_end_ms, + extra_params). No KeepAlive
is sent: the caller pushes every played ear frame, silence included.

Dograh parity (fix-40). Dograh runs Deepgram through pipecat with endpointing=100,
interim_results=true, punctuate=true, model nova-3-general, language multi, and takes
turn-taking from its local Silero VAD (stop_secs 0.2), never from Deepgram: when the VAD
reports the user stopped, pipecat sends `{"type":"Finalize"}` and Deepgram flushes the
final at once (`from_finalize: true`; pipecat services/deepgram/stt.py). The harness
does the same: `endpointing` defaults to 100 (was 10; 10 ms split finals at every short
gap and, measured live 2026-11-07, still left finals to arrive once per 3-5 s chunk on
continuous speech), and `DeepgramStreamer.finalize()` sends Finalize mid-stream; the
simulated caller calls it on every agent "stop" of the ear segmenter and calibrate does
it at every detected speech end (TTFS, see calibrate.refasr_stage). Differences kept on
purpose: punctuate=false (the normalizer and matching need raw spoken words; Dograh
punctuates), and model/language stay this wrapper's (nova-3 / en), unchanged here.

Release rule (fix-24). Final words: every `Results` with `is_final: true` releases its
words with `final=True`; they never change and are never released again. Provisional
words (only on `stream(provisional=True)`; `stream()` and `transcribe()` keep the
finals-only rule): Deepgram's interim Results (`is_final: false`) for the current chunk
are cumulative (each repeats the chunk's words so far, times and text may move), and the
chunk ends at the next `is_final` Results. Words are tracked by position index in the
chunk: on an interim Results, word i is released with `final=False` the first time index
i appears, and released again with `final=False` when its text differs from the text
last released at i (a correction); a word whose only change is its times is not
re-released. An `is_final` Results releases the chunk's words with `final=True` and
starts a new chunk. A Results with `from_finalize: true` (the reply to finalize() or to
finish()'s Finalize) is an `is_final` Results and goes through the same final release.
So a spoken word arrives as one or more provisional words (early)
and exactly once as a final word (settled). Transcripts keep only `final` words
(`engine.settled`); latency may be read from the first provisional release. Interim
results also carry `metadata.model_uuid` (version rule below). Word times are
Deepgram's `start`/`end` (s from stream start) as ms ints; text is `word` (never
`punctuated_word`), conf is `confidence`.

Version rule: `version` is f"{model}/{pinned_model_uuid or 'unpinned'}" until the first
Results message of any stream, then f"{model}/{model_uuid}" with `model_info` kept on the
instance. The API offers no pin beyond `version=latest` (providers.md §8), so with
`pinned_model_uuid` set a stream that reports another uuid raises RefASRVersionMismatch
from push()/finish(): the run must stop, never continue on another model.

Errors: connect failure, HTTP 4xx on the handshake, a non-JSON message, an unexpected
close or a timeout raise RefASRTransportError with a one-line reason; the API key never
appears in any message. The finish() timeout names the last server message type
(`Results interim|is_final|from_finalize`, `Metadata`, ...) and the ms since it arrived,
or says none arrived (fix-41). `DeepgramStreamer.messages` keeps every server message with
an added `_arrival_ms` (ms since the streamer was created, perf_counter) for the
calibration diagnostics log (calibrate.refasr_result).

Cost: Nova-3 monolingual streaming $0.0077 per audio minute pay-as-you-go
(deepgram.com/pricing, checked 2026-11-06; docs/providers.md §8), from
configs/prices/deepgram_nova3.yaml. Every audio second pushed is billed.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from typing import Any
from urllib.parse import urlencode

import numpy as np

from duplexbench.refasr.candidates import CandidateUnavailable, LINE_RATE, frame_pcm, has_module
from duplexbench.refasr.engine import Word

PIP = "websockets"
DEFAULT_KEY_ENV = "DEEPGRAM_API_KEY"
PRICE_REF = "deepgram_nova3"
FRAME_SAMPLES = LINE_RATE // 50          # 20 ms at 8 kHz = 160 samples
_MS_PER_S = 1_000
_S_PER_MIN = 60.0                        # threshold-ok: unit conversion s per min


class RefASRTransportError(RuntimeError):
    """The Deepgram stream failed (connect, HTTP status, bad message, close, timeout)."""


class RefASRVersionMismatch(RuntimeError):
    """A stream reported a model_uuid other than the pinned one (decision B11)."""


def usd_per_audio_min() -> float:
    """Streaming price per audio minute from configs/prices/deepgram_nova3.yaml."""
    from duplexbench.systems import load_prices
    return float(load_prices(PRICE_REF)["rates"]["stream_per_audio_min"])


def projected_usd(audio_s: float) -> float:
    return float(audio_s) / _S_PER_MIN * usd_per_audio_min()


def probe(api_key_env: str = DEFAULT_KEY_ENV) -> str | None:
    """None when `websockets` imports and the key env var is set; else the reason."""
    if not has_module("websockets"):
        return f"pip install {PIP}"
    if not os.environ.get(api_key_env, "").strip():
        return f"set {api_key_env}"
    return None


def _ms(s: Any) -> int:
    return int(round(float(s) * _MS_PER_S))


class DeepgramASR:
    name = "deepgram"
    streaming = True
    word_timestamps = True
    paid = True                      # refasr-bench / calibrate print cost and need --yes
    realtime_input = True            # calibrate paces pushes at real time (network engine)

    def __init__(self, model: str = "nova-3", language: str = "en", *,
                 api_key_env: str = DEFAULT_KEY_ENV, api_key: str | None = None,
                 url: str = "wss://api.deepgram.com/v1/listen", endpointing: int = 100,
                 utterance_end_ms: int | None = None, pinned_model_uuid: str | None = None,
                 connect_timeout_s: float = 10.0, finish_timeout_s: float = 5.0,
                 extra_params: dict[str, Any] | None = None) -> None:
        if not has_module("websockets"):
            raise CandidateUnavailable(f"websockets not installed: pip install {PIP}")
        key = (api_key or os.environ.get(api_key_env, "")).strip()
        if not key:
            raise CandidateUnavailable(f"set {api_key_env} (Deepgram API key) in .env or the "
                                       "environment; no key, no reference-ASR stream")
        self._key = key
        self.model = model
        self.language = language
        self.url = url
        self.endpointing = endpointing
        self.utterance_end_ms = utterance_end_ms
        self.pinned_model_uuid = pinned_model_uuid
        self.connect_timeout_s = float(connect_timeout_s)
        self.finish_timeout_s = float(finish_timeout_s)
        self.extra_params = dict(extra_params or {})
        self.model_uuid: str | None = None
        self.model_info: dict | None = None
        self.version = f"{model}/{pinned_model_uuid or 'unpinned'}"
        self._lock = threading.Lock()

    # -- config -------------------------------------------------------------------------
    def query(self) -> dict[str, str]:
        q: dict[str, Any] = {"model": self.model, "language": self.language,
                             "encoding": "linear16", "sample_rate": LINE_RATE, "channels": 1,
                             "interim_results": "true", "endpointing": self.endpointing,
                             "punctuate": "false", "smart_format": "false",
                             "numerals": "false"}
        if self.utterance_end_ms is not None:
            q["utterance_end_ms"] = self.utterance_end_ms
        q.update(self.extra_params)
        return {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in q.items()}

    def full_url(self) -> str:
        return f"{self.url}?{urlencode(self.query())}"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._key}"}

    def scrub(self, msg: str) -> str:
        """One line, key removed."""
        return " ".join(str(msg).replace(self._key, "***").split())

    def _observe(self, uuid: str | None, info: Any) -> str | None:
        """Record the uuid from a Results message; a mismatch reason when pinned differs."""
        if not uuid:
            return None
        with self._lock:
            if self.model_uuid is None:
                self.model_uuid = str(uuid)
                self.model_info = dict(info) if isinstance(info, dict) else None
                self.version = f"{self.model}/{self.model_uuid}"
        if self.pinned_model_uuid and str(uuid) != self.pinned_model_uuid:
            return (f"deepgram model_uuid {uuid} != pinned_model_uuid "
                    f"{self.pinned_model_uuid}: the reference ASR changed; stop the run "
                    "(decision B11)")
        return None

    # -- RefASR -------------------------------------------------------------------------
    def stream(self, provisional: bool = False) -> "DeepgramStreamer":
        """One streaming connection; `provisional=True` also releases interim words
        (final=False) under the release rule in the module docstring."""
        return DeepgramStreamer(self, provisional=provisional)

    def transcribe(self, pcm8k: np.ndarray) -> list[Word]:
        x = np.asarray(pcm8k, dtype=np.int16).ravel()
        st = self.stream()
        out: list[Word] = []
        try:
            for i in range(0, len(x), FRAME_SAMPLES):
                out += st.push(x[i:i + FRAME_SAMPLES])
            out += st.finish()
        finally:
            st.close()
        return out


class DeepgramStreamer:
    """One Deepgram streaming connection; see the module docstring for the rules."""

    def __init__(self, asr: DeepgramASR, provisional: bool = False) -> None:
        self.asr = asr
        self.provisional = bool(provisional)
        self._chunk: list[str] = []            # text last released per index, current chunk
        self._lock = threading.Lock()          # serialises push/finish callers
        self._words_lock = threading.Lock()    # reader thread <-> caller thread
        self._ready: list[Word] = []
        self._error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._q: asyncio.Queue | None = None
        self._ws: Any = None
        self._closing = False
        self._done = threading.Event()         # reader ended (socket closed or failed)
        self._metadata = threading.Event()     # Metadata after CloseStream
        self._finalized = threading.Event()    # a from_finalize Results arrived
        self._from_finalize_n = 0              # from_finalize Results received
        self._finished = False
        self.sent_bytes = 0
        self.finalize_count = 0                # mid-stream Finalize messages sent (fix-40)
        # every server message (diagnostics); fix-41: each entry also carries
        # "_arrival_ms", ms since this streamer was created (perf_counter) when it arrived
        self.messages: list[dict] = []
        self.t0_ns = time.perf_counter_ns()    # stream start for "_arrival_ms"
        self._last_msg: tuple[str, int] | None = None   # (type, perf_counter_ns) of the latest

    # -- caller side ---------------------------------------------------------------------
    def push(self, frame: np.ndarray | bytes) -> list[Word]:
        with self._lock:
            if self._finished:
                raise RuntimeError("push after finish")
            self._raise_pending()
            if self._loop is None:
                self._open()
            data = frame_pcm(frame).astype("<i2").tobytes()
            if data:
                self.sent_bytes += len(data)
                self._loop.call_soon_threadsafe(self._q.put_nowait, data)
            self._raise_pending()
            return self._take()

    def finalize(self) -> None:
        """Send `{"type":"Finalize"}` mid-stream and return at once (fix-40). Deepgram
        flushes the audio received so far into an `is_final` Results with
        `from_finalize: true`; its words are released as final words by later push() /
        finish() calls. The stream stays open (Deepgram accepts repeated Finalize).
        A stream that was never pushed has nothing to finalize: no message, no count."""
        with self._lock:
            if self._finished:
                raise RuntimeError("finalize after finish")
            self._raise_pending()
            if self._loop is None:
                return
            self.finalize_count += 1
            self._loop.call_soon_threadsafe(self._q.put_nowait,
                                            json.dumps({"type": "Finalize"}))

    def finish(self) -> list[Word]:
        with self._lock:
            if self._finished:
                return []
            self._finished = True
            if self._loop is None:              # never pushed: no connection was opened
                return []
            try:
                self._raise_pending()
                self._closing = True           # before CloseStream: its close is expected
                n_fin = self._from_finalize_n  # replies to earlier finalize() calls excluded
                for msg in ({"type": "Finalize"}, {"type": "CloseStream"}):
                    self._loop.call_soon_threadsafe(self._q.put_nowait, json.dumps(msg))
                if (not self._wait_end(self.asr.finish_timeout_s)
                        and self._from_finalize_n <= n_fin):
                    raise RefASRTransportError(
                        f"deepgram finish timeout: no final Results within "
                        f"{self.asr.finish_timeout_s:g} s; {self.last_message_info()}")
                self._raise_pending()
                return self._take()
            finally:
                self._shutdown()

    def last_message_info(self) -> str:
        """fix-41: the latest server message type and the ms since it arrived, or that none
        arrived (a stall after messages vs a stream that never answered)."""
        last = self._last_msg
        if last is None:
            return "no server message received on this stream"
        typ, t_ns = last
        ago = (time.perf_counter_ns() - t_ns) / 1_000_000
        return (f"last message {typ} {ago:.0f} ms ago "
                f"({len(self.messages)} messages received)")

    def close(self) -> None:
        """Drop the connection without waiting for results (idempotent)."""
        with self._lock:
            self._finished = True
            self._shutdown()

    def _wait_end(self, timeout: float) -> bool:
        """Socket closed or Metadata received (all Results precede both)."""
        import time
        deadline = time.perf_counter() + timeout
        while True:
            if self._done.is_set() or self._metadata.is_set():
                return True
            left = deadline - time.perf_counter()
            if left <= 0:
                return False
            self._done.wait(min(left, 0.01))

    def _take(self) -> list[Word]:
        with self._words_lock:
            out, self._ready = self._ready, []
        return out

    def _raise_pending(self) -> None:
        err = self._error
        if err is not None:
            self._finished = True
            self._shutdown()
            raise err

    def _fail(self, err: BaseException) -> None:
        if self._error is None:
            self._error = err
        self._done.set()

    # -- connection thread ---------------------------------------------------------------
    def _open(self) -> None:
        from duplexbench import _loop
        ready = threading.Event()

        def main() -> None:
            loop = _loop.new_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._q = asyncio.Queue()
            ready.set()
            loop.run_forever()
            loop.close()

        self._thread = threading.Thread(target=main, name="deepgram-refasr", daemon=True)
        self._thread.start()
        ready.wait()
        fut = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        try:
            fut.result(timeout=self.asr.connect_timeout_s + 1.0)
        except Exception as exc:  # noqa: BLE001 - normalised below
            fut.cancel()
            err = exc if isinstance(exc, RefASRTransportError) else RefASRTransportError(
                self.asr.scrub(f"deepgram connect failed: {type(exc).__name__}"))
            self._fail(err)
            self._raise_pending()

    async def _connect(self) -> None:
        from websockets.asyncio.client import connect
        from websockets.exceptions import InvalidStatus
        a = self.asr
        try:
            self._ws = await connect(a.full_url(), additional_headers=a.headers(),
                                     open_timeout=a.connect_timeout_s, max_size=None,
                                     ping_interval=None, compression=None)
        except InvalidStatus as exc:
            code = getattr(getattr(exc, "response", None), "status_code", "?")
            raise RefASRTransportError(f"deepgram connect refused: HTTP {code}") from None
        except (TimeoutError, asyncio.TimeoutError):
            raise RefASRTransportError(
                f"deepgram connect timeout after {a.connect_timeout_s:g} s") from None
        except Exception as exc:  # noqa: BLE001 - OSError, handshake errors
            raise RefASRTransportError(
                a.scrub(f"deepgram connect failed: {type(exc).__name__}: {exc}")) from None
        asyncio.get_running_loop().create_task(self._sender())
        asyncio.get_running_loop().create_task(self._reader())

    async def _sender(self) -> None:
        try:
            while True:
                item = await self._q.get()
                if item is None:
                    return
                await self._ws.send(item)
        except Exception as exc:  # noqa: BLE001
            self._fail(RefASRTransportError(
                self.asr.scrub(f"deepgram send failed: {type(exc).__name__}")))

    async def _reader(self) -> None:
        from websockets.exceptions import ConnectionClosed
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    raise RefASRTransportError("deepgram sent a non-JSON message (binary frame)")
                try:
                    msg = json.loads(raw)
                except ValueError:
                    raise RefASRTransportError("deepgram sent a non-JSON message") from None
                if not isinstance(msg, dict):
                    raise RefASRTransportError("deepgram sent a non-object JSON message")
                now = time.perf_counter_ns()
                typ = str(msg.get("type", "?"))
                if typ == "Results":
                    typ += (" from_finalize" if msg.get("from_finalize")
                            else " is_final" if msg.get("is_final") else " interim")
                self._last_msg = (typ, now)
                self.messages.append(dict(msg, _arrival_ms=round((now - self.t0_ns) / 1e6, 3)))
                self._on_message(msg)
            rc = getattr(self._ws, "close_code", None)
            if not self._closing and rc not in (None, 1000):  # threshold-ok: WS normal-close code
                self._fail(RefASRTransportError(
                    self.asr.scrub(f"deepgram closed the stream: code {rc} "
                                   f"{getattr(self._ws, 'close_reason', '')}")))
            elif not self._closing:
                self._fail(RefASRTransportError("deepgram closed the stream before finish()"))
        except RefASRTransportError as exc:
            self._fail(exc)
        except RefASRVersionMismatch as exc:
            self._fail(exc)
        except ConnectionClosed as exc:
            if not self._closing:
                rcvd = getattr(exc, "rcvd", None)
                code = getattr(rcvd, "code", "?")
                reason = getattr(rcvd, "reason", "") or ""
                self._fail(RefASRTransportError(
                    self.asr.scrub(f"deepgram closed the stream: code {code} {reason}")))
        except Exception as exc:  # noqa: BLE001
            self._fail(RefASRTransportError(
                self.asr.scrub(f"deepgram read failed: {type(exc).__name__}")))
        finally:
            self._done.set()

    def _on_message(self, msg: dict) -> None:
        typ = msg.get("type")
        if typ == "Metadata":
            self._metadata.set()
            return
        if typ != "Results":
            return
        meta = msg.get("metadata") or {}
        why = self.asr._observe(meta.get("model_uuid"), meta.get("model_info"))
        if why:
            raise RefASRVersionMismatch(why)
        alts = (msg.get("channel") or {}).get("alternatives") or [{}]
        if msg.get("is_final"):
            words = self._words(alts[0], final=True)
            self._chunk = []                   # the next interim starts a new chunk
            with self._words_lock:
                self._ready += words
        elif self.provisional:
            out = []
            for i, w in enumerate(self._words(alts[0], final=False)):
                if i >= len(self._chunk):
                    self._chunk.append(w.text)
                    out.append(w)
                elif w.text != self._chunk[i]:
                    self._chunk[i] = w.text     # correction: released again, provisional
                    out.append(w)
            if out:
                with self._words_lock:
                    self._ready += out
        if msg.get("from_finalize"):
            self._from_finalize_n += 1
            self._finalized.set()

    @staticmethod
    def _words(alt: dict, *, final: bool) -> list[Word]:
        return [Word(str(w["word"]), _ms(w["start"]), _ms(w["end"]),
                     float(w.get("confidence", 1.0)), final)
                for w in (alt.get("words") or []) if str(w.get("word", "")).strip()]

    def _shutdown(self) -> None:
        loop, th = self._loop, self._thread
        if loop is None or th is None or not th.is_alive():
            return

        async def down() -> None:
            if self._q is not None:
                self._q.put_nowait(None)
            if self._ws is not None:
                try:
                    await asyncio.wait_for(self._ws.close(), 1.0)
                except Exception:  # noqa: BLE001 - already gone
                    pass
            for t in asyncio.all_tasks():
                if t is not asyncio.current_task():
                    t.cancel()

        try:
            asyncio.run_coroutine_threadsafe(down(), loop).result(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        loop.call_soon_threadsafe(loop.stop)
        if th is not threading.current_thread():
            th.join(timeout=3.0)
