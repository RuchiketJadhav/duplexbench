"""Fish Audio text-to-speech client with word timestamps (harness task tts-fish, decision B13).

The first real `TTSClient` (stimuli/tts.py). It generates Track A / D caller stimulus
clips and gives the simulated caller (simulator/audio.py `CallerVoice`) a real voice.
API facts come only from docs/providers.md §10 (docs.fish.audio, checked 2026-11-06).

    tts = FishTTS(model="drama-3", voices=load_voices("configs/voices/fish.yaml"),
                  default_voice="delia")                    # key from FISH_API_KEY
    pcm, sr, words = tts.synthesize("my order number is 4 4 7", "delia", 1.0)
    tts = FishTTS.from_config({"vendor": "fish", "model": "drama-3",
                               "voices_file": "configs/voices/fish.yaml", "default_voice": "delia"})
    usd_per_1k_chars("s2.1-pro")                            # 0.015

Request (one `POST {base_url}/v1/tts/stream/with-timestamp` per text):
  headers  `Authorization: Bearer <key>`, `Content-Type: application/json`, `model: <model>`
  body     {text, reference_id, format: "pcm", sample_rate, latency,
            prosody: {speed: rate, volume: 0, normalize_loudness: true},
            normalize, language, temperature, top_p}
  `reference_id` = voices[voice]; else `voice` itself when it is a 32-hex Fish id; else
  voices[default_voice] (or default_voice itself when it is a raw id); else ValueError.

Response: Server-Sent Events; each `data:` payload is JSON
  {audio_base64, content, chunk_seq, chunk_audio_offset_sec, alignment | null}.
  * Audio: every event's base64 audio is decoded and concatenated in arrival order,
    then read as little-endian int16 mono at `sample_rate` (format pcm is raw 16-bit).
  * Alignment snapshot rule: `alignment` is cumulative per `chunk_seq`, so the LATEST
    non-null snapshot for a chunk_seq REPLACES the earlier one (never appended). A
    trailing event with empty audio may carry the final snapshot; it still counts.
  * Words: every kept snapshot's `alignment.segments[] = {text, start, end}` (seconds
    from the start of that text chunk) becomes
    Word(text, round((chunk_audio_offset_sec + start) * 1000), round((... + end) * 1000)),
    sorted by start_ms.
  Non-2xx, a transport error, malformed SSE / JSON, odd byte count or no audio ->
  `TTSError` with the status and the first 300 characters of the body; the API key
  never appears in a message. A missing key -> `TTSUnavailable` before any request.

Model header: Fish falls back SILENTLY to `s2.1-pro` when the `model` header is missing
or misspelled (providers.md §10), so a typo would bill and voice a different model with
no error. The client therefore validates `model` against KNOWN_MODELS and refuses others.
Whether `drama-3` serves the with-timestamp route is unconfirmed until the first paid
smoke ([Assumption] in providers.md §10).

Price (configs/prices/fish_audio.yaml; source
https://docs.fish.audio/developer-guide/models-pricing/pricing-and-rate-limits, checked
2026-11-06): USD per 1M UTF-8 bytes of input text: drama-3 $50.00 (launch price $10.00
until 2026-11-30; the list price is used), s2.1-pro $15.00, s2.1-pro-free $0, s2-pro
$15.00. `usd_per_1k_chars` treats 1 character as 1 byte, exact for ASCII English text
(non-ASCII characters are 2-4 bytes, so the projection under-counts those).

Caching: `stimuli.tts.synthesize` keys its cache on the client's `version`
("fish/<model>") when present, so clips from different Fish models never collide.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import yaml

from duplexbench.stimuli.tts import Word

PRICE_REF = "fish_audio"
KNOWN_MODELS = frozenset({"drama-3", "s2.1-pro", "s2.1-pro-free", "s2-pro"})
DEFAULT_VOICES_FILE = Path("configs") / "voices" / "fish.yaml"
_ROUTE = "/v1/tts/stream/with-timestamp"
_VOICE_ID = re.compile(r"^[0-9a-f]{32}$")
_BODY_PREVIEW = 300               # threshold-ok: error-message body excerpt length
_MS_PER_S = 1000                  # threshold-ok: unit conversion ms per s
_BYTES_PER_MILLION = 1_000_000    # threshold-ok: price unit per 1M bytes
_CHARS_PER_UNIT = 1000            # threshold-ok: prices are projected per 1k characters


class TTSUnavailable(RuntimeError):
    """The client cannot be built (no API key)."""


class TTSError(RuntimeError):
    """A Fish Audio request failed or returned an unusable stream."""


def _is_voice_id(s: str | None) -> bool:
    return bool(s) and bool(_VOICE_ID.match(str(s)))


def usd_per_1k_chars(model: str, prices_dir: Path | None = None) -> float:
    """USD per 1,000 characters for `model`, from configs/prices/fish_audio.yaml.

    The file prices per 1M UTF-8 bytes; 1 character = 1 byte for ASCII text, so
    per-1k-chars = rate / 1000."""
    from duplexbench.systems import load_prices
    rates = load_prices(PRICE_REF, prices_dir)["rates"]
    if model not in rates:
        raise ValueError(f"no Fish Audio price for model {model!r} in {PRICE_REF}.yaml "
                         f"(priced: {sorted(rates)})")
    return float(rates[model]) * _CHARS_PER_UNIT / _BYTES_PER_MILLION


def _resolve_path(p: str | Path) -> Path:
    path = Path(p)
    if path.is_absolute() or path.is_file():
        return path
    from duplexbench.systems import REPO_ROOT
    return REPO_ROOT / path


def load_voices(path: str | Path) -> dict[str, str]:
    """Read a voices file (`voices: {name: 32-hex id}`); relative paths fall back to the repo root."""
    p = _resolve_path(path)
    if not p.is_file():
        raise ValueError(f"voices file not found: {p}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    voices = raw.get("voices") or {}
    if not isinstance(voices, dict):
        raise ValueError(f"{p}: 'voices' must be a mapping of name -> Fish voice id")
    out: dict[str, str] = {}
    for name, vid in voices.items():
        if not _is_voice_id(str(vid)):
            raise ValueError(f"{p}: voice {name!r} id {vid!r} is not a 32-hex Fish voice id")
        out[str(name)] = str(vid)
    return out


def resolve_voice(voice: str, voices: dict[str, str], default_voice: str | None) -> str:
    """Harness voice name -> Fish `reference_id` (rule in the module docstring)."""
    if voice in voices:
        return voices[voice]
    if _is_voice_id(voice):
        return voice
    if default_voice is not None:
        if default_voice in voices:
            return voices[default_voice]
        if _is_voice_id(default_voice):
            return default_voice
        raise ValueError(f"default_voice {default_voice!r} is neither in the voices map "
                         "nor a 32-hex Fish voice id")
    raise ValueError(f"unknown voice {voice!r}: not in the voices map "
                     f"({sorted(voices)}), not a 32-hex Fish voice id, and no default_voice")


def _sse_payloads(lines: Iterable[str]) -> Iterator[str]:
    """Yield each SSE event's data (multi-line `data:` fields joined by newline)."""
    buf: list[str] = []
    for line in lines:
        line = line.rstrip("\r")
        if line == "":
            if buf:
                yield "\n".join(buf)
                buf = []
            continue
        if line.startswith(":"):
            continue                                # SSE comment / keep-alive
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            buf.append(value)
        # event:, id:, retry: carry nothing the harness uses
    if buf:
        yield "\n".join(buf)


def parse_stream(payloads: Iterable[str]) -> tuple[bytes, list[Word]]:
    """SSE data payloads -> (raw pcm bytes, words). Raises TTSError on malformed input."""
    audio = bytearray()
    snaps: dict[int, tuple[float, dict]] = {}       # chunk_seq -> (offset_s, latest alignment)
    offsets: dict[int, float] = {}                   # chunk_seq -> first offset seen
    n_events = 0
    for data in payloads:
        if data.strip() == "[DONE]":
            continue
        try:
            ev = json.loads(data)
        except json.JSONDecodeError as e:
            raise TTSError(f"malformed SSE event (not JSON): {data[:_BODY_PREVIEW]!r}") from e
        if not isinstance(ev, dict):
            raise TTSError(f"malformed SSE event (not an object): {data[:_BODY_PREVIEW]!r}")
        n_events += 1
        b64 = ev.get("audio_base64") or ""
        if b64:
            try:
                audio += base64.b64decode(b64, validate=True)
            except (binascii.Error, ValueError, TypeError) as e:
                raise TTSError(f"malformed audio_base64 in SSE event: "
                               f"{data[:_BODY_PREVIEW]!r}") from e
        seq = ev.get("chunk_seq")
        off = ev.get("chunk_audio_offset_sec")
        if seq is None:
            seq = 0
        try:
            seq = int(seq)
            if off is not None:
                off = float(off)
                offsets.setdefault(seq, off)
        except (TypeError, ValueError) as e:
            raise TTSError(f"malformed chunk_seq / offset: {data[:_BODY_PREVIEW]!r}") from e
        al = ev.get("alignment")
        if al is not None:
            if not isinstance(al, dict) or not isinstance(al.get("segments", []), list):
                raise TTSError(f"malformed alignment: {data[:_BODY_PREVIEW]!r}")
            snaps[seq] = (off if off is not None else offsets.get(seq, 0.0), al)   # replace
    if n_events == 0:
        raise TTSError("empty SSE stream: no data events")
    words: list[Word] = []
    for seq, (off, al) in snaps.items():
        for seg in al.get("segments") or []:
            try:
                text = str(seg["text"]).strip()
                a = round((off + float(seg["start"])) * _MS_PER_S)
                b = round((off + float(seg["end"])) * _MS_PER_S)
            except (KeyError, TypeError, ValueError) as e:
                raise TTSError(f"malformed alignment segment in chunk {seq}: {seg!r}") from e
            if text:
                words.append(Word(text=text, start_ms=int(a), end_ms=int(b)))
    words.sort(key=lambda w: (w.start_ms, w.end_ms))
    return bytes(audio), words


class FishTTS:
    """Fish Audio `TTSClient` over the SSE with-timestamp route (see module docstring)."""

    vendor = "fish"

    def __init__(self, *, model: str = "drama-3", api_key: str | None = None,
                 api_key_env: str = "FISH_API_KEY", base_url: str = "https://api.fish.audio",
                 voices: dict[str, str] | None = None, default_voice: str | None = None,
                 sample_rate: int = 16000, latency: str = "balanced",
                 temperature: float = 0.7,    # threshold-ok: Fish sampling default (providers.md §10)
                 top_p: float = 0.7,          # threshold-ok: Fish sampling default (providers.md §10)
                 normalize: bool = True, language: str = "en",
                 timeout_s: float = 60.0, transport: httpx.BaseTransport | None = None) -> None:
        if model not in KNOWN_MODELS:
            raise ValueError(f"unknown Fish Audio model {model!r}; known: {sorted(KNOWN_MODELS)} "
                             "(Fish silently falls back to s2.1-pro on an unknown model header)")
        key = api_key if api_key is not None else os.environ.get(api_key_env)
        if not key:
            raise TTSUnavailable(f"Fish Audio API key missing: set {api_key_env}")
        self._key = key
        self.model = model
        self.version = f"fish/{model}"
        self.base_url = base_url.rstrip("/")
        self.voices = dict(voices or {})
        self.default_voice = default_voice
        self.sample_rate = int(sample_rate)
        self.latency = latency
        self.temperature = float(temperature)
        self.top_p = float(top_p)
        self.normalize = bool(normalize)
        self.language = language
        self.timeout_s = float(timeout_s)
        self._transport = transport
        self.calls = 0
        self.sent_bytes = 0                       # UTF-8 bytes of text sent (the billed unit)

    @classmethod
    def from_config(cls, cfg: dict, *, transport: httpx.BaseTransport | None = None) -> FishTTS:
        """Build from a run-config block such as `simulator.tts`:
        {vendor: fish, model, voices_file, default_voice, sample_rate, latency, api_key_env}."""
        cfg = dict(cfg or {})
        vendor = cfg.pop("vendor", "fish")
        if vendor != "fish":
            raise ValueError(f"FishTTS.from_config got vendor {vendor!r}")
        voices_file = cfg.pop("voices_file", None)
        voices = load_voices(voices_file) if voices_file else {}
        allowed = {"model", "default_voice", "sample_rate", "latency", "api_key_env",
                   "base_url", "temperature", "top_p", "normalize", "language", "timeout_s"}
        unknown = set(cfg) - allowed
        if unknown:
            raise ValueError(f"unknown simulator.tts keys for fish: {sorted(unknown)}")
        return cls(voices=voices, transport=transport, **cfg)

    def reference_id(self, voice: str) -> str:
        return resolve_voice(voice, self.voices, self.default_voice)

    def request_body(self, text: str, voice: str, rate: float) -> dict[str, Any]:
        return {"text": text, "reference_id": self.reference_id(voice), "format": "pcm",
                "sample_rate": self.sample_rate, "latency": self.latency,
                "prosody": {"speed": float(rate), "volume": 0, "normalize_loudness": True},
                "normalize": self.normalize, "language": self.language,
                "temperature": self.temperature, "top_p": self.top_p}

    def _scrub(self, s: str) -> str:
        return s.replace(self._key, "***") if self._key else s

    def synthesize(self, text: str, voice: str, rate: float) -> tuple[np.ndarray, int, list[Word]]:
        body = self.request_body(text, voice, rate)          # ValueError on unknown voice
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                   "model": self.model}
        url = self.base_url + _ROUTE
        self.calls += 1
        self.sent_bytes += len(text.encode("utf-8"))
        try:
            with httpx.Client(transport=self._transport, timeout=self.timeout_s) as client:
                with client.stream("POST", url, headers=headers, json=body) as r:
                    if not r.is_success:
                        raw = r.read().decode("utf-8", "replace")
                        raise TTSError(self._scrub(
                            f"Fish Audio {r.status_code} from {_ROUTE} (model {self.model}): "
                            f"{raw[:_BODY_PREVIEW]}"))
                    audio, words = parse_stream(_sse_payloads(r.iter_lines()))
        except TTSError as e:
            raise TTSError(self._scrub(str(e))) from None
        except httpx.HTTPError as e:
            raise TTSError(self._scrub(f"Fish Audio request failed ({type(e).__name__}): {e}")
                           ) from None
        if not audio:
            raise TTSError(f"Fish Audio stream from {_ROUTE} (model {self.model}) carried no audio")
        if len(audio) % 2:
            raise TTSError(f"Fish Audio pcm has an odd byte count ({len(audio)}); "
                           "expected 16-bit samples")
        pcm = np.frombuffer(audio, dtype="<i2").astype(np.int16)
        return pcm, self.sample_rate, words
