"""tts-fish: FishTTS request/response mapping over httpx.MockTransport (no network, $0)."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import numpy as np
import pytest
import typer
from typer.testing import CliRunner

from duplexbench import cli_stimuli
from duplexbench.stimuli import fish_tts
from duplexbench.stimuli.fake_tts import FakeTTS
from duplexbench.stimuli.fish_tts import (FishTTS, TTSError, TTSUnavailable, load_voices,
                                          parse_stream, usd_per_1k_chars)
from duplexbench.stimuli.tts import TTSClient, Word, cache_key, client_id, synthesize
from duplexbench.stimuli.vendors import build_tts

KEY = "sk-fish-SECRET-123"
DELIA = "c70cfb98867a4bfb9ae20d2401257b92"
ADRIAN = "bf322df2096a46f18c579d0baa36f41d"
RAW_ID = "0123456789abcdef0123456789abcdef"
VOICES = {"delia": DELIA, "adrian": ADRIAN}
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev"


def _b64(samples) -> str:
    return base64.b64encode(np.asarray(samples, dtype="<i2").tobytes()).decode()


def _ev(audio=None, seq=0, off=0.0, alignment=None, content="") -> dict:
    return {"audio_base64": _b64(audio) if audio is not None else "", "content": content,
            "chunk_seq": seq, "chunk_audio_offset_sec": off, "alignment": alignment}


def _al(*segs) -> dict:
    return {"segments": [{"text": t, "start": a, "end": b} for t, a, b in segs]}


def _sse(*events) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


SCRIPT = _sse(
    _ev([1, 2, 3], seq=0, off=0.0, alignment=_al(("hello", 0.0, 0.3))),
    _ev([4, 5], seq=0, off=0.0, alignment=_al(("hello", 0.01, 0.31), ("there", 0.4, 0.7))),
    _ev([6, 7, 8], seq=1, off=1.25, alignment=None),
    _ev([9], seq=1, off=1.25, alignment=_al(("how", 0.0, 0.2))),
    _ev(None, seq=1, off=1.25, alignment=_al(("how", 0.05, 0.2), ("are", 0.3, 0.45))),
)


class Recorder:
    def __init__(self, response: httpx.Response | bytes = SCRIPT):
        self.requests: list[httpx.Request] = []
        self.response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, httpx.Response):
            return self.response
        return httpx.Response(200, content=self.response,
                              headers={"content-type": "text/event-stream"})

    def body(self, i=-1) -> dict:
        return json.loads(self.requests[i].content)


def _client(rec, **kw) -> FishTTS:
    kw.setdefault("voices", VOICES)
    return FishTTS(api_key=KEY, transport=httpx.MockTransport(rec), **kw)


# -- request ---------------------------------------------------------------------------
def test_is_a_client_with_vendor_and_version():
    c = _client(Recorder(), model="s2.1-pro")
    assert isinstance(c, TTSClient)
    assert c.vendor == "fish" and c.version == "fish/s2.1-pro" and client_id(c) == "fish/s2.1-pro"
    assert client_id(FakeTTS()) == "fake"


def test_request_headers_and_body():
    rec = Recorder()
    _client(rec, model="drama-3").synthesize("hello there. how are", "delia", 1.15)
    req = rec.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.fish.audio/v1/tts/stream/with-timestamp"
    assert req.headers["authorization"] == f"Bearer {KEY}"
    assert req.headers["content-type"] == "application/json"
    assert req.headers["model"] == "drama-3"
    assert rec.body() == {
        "text": "hello there. how are", "reference_id": DELIA, "format": "pcm",
        "sample_rate": 16000, "latency": "balanced",
        "prosody": {"speed": 1.15, "volume": 0, "normalize_loudness": True},
        "normalize": True, "language": "en", "temperature": 0.7, "top_p": 0.7}


def test_voice_resolution():
    rec = Recorder()
    c = _client(rec, default_voice="adrian")
    c.synthesize("x", RAW_ID, 1.0)
    c.synthesize("x", "voice-f1", 1.0)
    c.synthesize("x", "delia", 1.0)
    assert [rec.body(i)["reference_id"] for i in range(3)] == [RAW_ID, ADRIAN, DELIA]


def test_unknown_voice_is_value_error_naming_it():
    rec = Recorder()
    with pytest.raises(ValueError, match="voice-f1"):
        _client(rec).synthesize("x", "voice-f1", 1.0)
    assert rec.requests == []


def test_unknown_model_refused():
    with pytest.raises(ValueError, match="s2.1-pr0"):
        FishTTS(api_key=KEY, model="s2.1-pr0")


def test_missing_key(monkeypatch):
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    with pytest.raises(TTSUnavailable, match="set FISH_API_KEY"):
        FishTTS()
    with pytest.raises(TTSUnavailable, match="set MY_FISH"):
        FishTTS(api_key_env="MY_FISH")
    monkeypatch.setenv("FISH_API_KEY", KEY)
    assert FishTTS()._key == KEY


# -- response ----------------------------------------------------------------------------
def test_audio_concatenated_and_words_from_latest_snapshots():
    pcm, sr, words = _client(Recorder()).synthesize("hello there. how are", "delia", 1.0)
    assert sr == 16000 and pcm.dtype == np.int16
    assert pcm.tolist() == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    # chunk 0: the second snapshot replaced the first; chunk 1: offset 1.25 s added and the
    # trailing empty-audio event's snapshot is final
    assert words == [Word("hello", 10, 310), Word("there", 400, 700),
                     Word("how", 1300, 1450), Word("are", 1550, 1700)]


def test_samples_split_across_chunks():
    raw = np.array([1000, -2000, 3000], dtype="<i2").tobytes()
    evs = [{"audio_base64": base64.b64encode(raw[:3]).decode(), "chunk_seq": 0,
            "chunk_audio_offset_sec": 0.0, "alignment": None},
           {"audio_base64": base64.b64encode(raw[3:]).decode(), "chunk_seq": 0,
            "chunk_audio_offset_sec": 0.0, "alignment": None}]
    pcm, _, words = _client(Recorder(_sse(*evs))).synthesize("x", "delia", 1.0)
    assert pcm.tolist() == [1000, -2000, 3000] and words == []


def test_parse_stream_ms_rounding_and_sorting():
    audio, words = parse_stream([json.dumps(_ev([0], seq=1, off=0.5, alignment=_al(("b", 0.0004, 0.0016)))),
                                 json.dumps(_ev([0], seq=0, off=0.0, alignment=_al(("a", 0.1234, 0.2006))))])
    assert words == [Word("a", 123, 201), Word("b", 500, 502)]
    assert len(audio) == 4


def test_sse_comments_event_lines_and_crlf_tolerated():
    body = (": keep-alive\r\nevent: message\r\ndata: " + json.dumps(_ev([7], alignment=_al(("hi", 0, 0.1))))
            + "\r\n\r\n").encode()
    pcm, _, words = _client(Recorder(body)).synthesize("hi", "delia", 1.0)
    assert pcm.tolist() == [7] and words == [Word("hi", 0, 100)]


# -- errors ------------------------------------------------------------------------------
def test_401_is_tts_error_without_key():
    rec = Recorder(httpx.Response(401, text=f'{{"message":"invalid token {KEY}"}}' + "x" * 600))
    with pytest.raises(TTSError) as ei:
        _client(rec).synthesize("x", "delia", 1.0)
    msg = str(ei.value)
    assert "401" in msg and KEY not in msg and "invalid token" in msg
    assert len(msg) < 450                                   # body cut to 300 chars


def test_malformed_sse_is_tts_error():
    with pytest.raises(TTSError, match="malformed"):
        _client(Recorder(b"data: {not json\n\n")).synthesize("x", "delia", 1.0)


def test_no_audio_is_tts_error():
    with pytest.raises(TTSError, match="no audio"):
        _client(Recorder(_sse(_ev(None, alignment=_al(("x", 0, 0.1)))))).synthesize("x", "delia", 1.0)
    with pytest.raises(TTSError, match="empty SSE"):
        _client(Recorder(b"")).synthesize("x", "delia", 1.0)


def test_transport_error_is_tts_error():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)
    with pytest.raises(TTSError, match="ConnectError"):
        _client(boom).synthesize("x", "delia", 1.0)


# -- config, factory, price, cache -------------------------------------------------------
def test_load_voices_seed_file():
    v = load_voices("configs/voices/fish.yaml")
    assert v == {"adrian": ADRIAN, "delia": DELIA, "sheila": "f3a2b90078d54a65af4b95f63bc7798e"}


def test_load_voices_rejects_bad_id(tmp_path):
    p = tmp_path / "v.yaml"
    p.write_text("voices:\n  bob: not-an-id\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bob"):
        load_voices(p)


CFG = {"vendor": "fish", "model": "drama-3", "voices_file": "configs/voices/fish.yaml",
       "default_voice": "delia", "sample_rate": 24000, "latency": "normal",
       "api_key_env": "FISH_TEST_KEY"}


def test_from_config(monkeypatch):
    monkeypatch.setenv("FISH_TEST_KEY", KEY)
    rec = Recorder()
    c = FishTTS.from_config(CFG, transport=httpx.MockTransport(rec))
    assert c.model == "drama-3" and c.default_voice == "delia" and c.sample_rate == 24000
    _, sr, _ = c.synthesize("x", "voice-m1", 1.0)
    b = rec.body()
    assert sr == 24000 and b["reference_id"] == DELIA and b["latency"] == "normal"
    with pytest.raises(ValueError, match="bogus"):
        FishTTS.from_config({**CFG, "bogus": 1})


def test_build_tts_dispatch(monkeypatch):
    monkeypatch.setenv("FISH_TEST_KEY", KEY)
    assert build_tts(None) is None and build_tts({}) is None and build_tts({"vendor": None}) is None
    assert isinstance(build_tts({"vendor": "fake"}), FakeTTS)
    assert build_tts({"vendor": "fake", "sample_rate": 8000}).sample_rate == 8000
    f = build_tts(CFG)
    assert isinstance(f, FishTTS) and f.version == "fish/drama-3"
    with pytest.raises(ValueError, match="acme"):
        build_tts({"vendor": "acme"})


def test_usd_per_1k_chars():
    assert usd_per_1k_chars("s2.1-pro") == pytest.approx(0.015)
    assert usd_per_1k_chars("s2-pro") == pytest.approx(0.015)
    assert usd_per_1k_chars("drama-3") == pytest.approx(0.05)
    assert usd_per_1k_chars("s2.1-pro-free") == 0.0
    with pytest.raises(ValueError):
        usd_per_1k_chars("s1")
    assert set(fish_tts.KNOWN_MODELS) == {"drama-3", "s2.1-pro", "s2.1-pro-free", "s2-pro"}


def test_cached_by_stimuli_synthesize(tmp_path):
    rec = Recorder()
    c = _client(rec)
    a = synthesize("hello there", "delia", 1.0, client=c, cache_dir=tmp_path)
    b = synthesize("hello there", "delia", 1.0, client=c, cache_dir=tmp_path)
    assert len(rec.requests) == 1
    assert a[0].tolist() == b[0].tolist() and a[1] == b[1] and a[2] == b[2]
    assert (tmp_path / f"{cache_key('fish/drama-3', 'delia', 1.0, 'hello there')}.wav").is_file()
    # another model never hits the drama-3 clip
    synthesize("hello there", "delia", 1.0, client=_client(rec, model="s2.1-pro"),
               cache_dir=tmp_path)
    assert len(rec.requests) == 2


# -- cli_stimuli -------------------------------------------------------------------------
def _app() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def _m() -> None: ...

    cli_stimuli.register(app)
    return app


def _gen(*extra):
    return CliRunner().invoke(_app(), ["stimuli", "generate", str(FIX), *extra])


def test_cli_no_vendor_refused_as_today(tmp_path):
    r = _gen("--usd-per-1k-chars", "0.03", "--out", str(tmp_path), "--yes")
    assert r.exit_code == cli_stimuli.EXIT_BLOCKED and "nothing was sent" in r.output


def test_cli_fish_projection_uses_price_file_and_refuses_without_yes(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(cli_stimuli, "fish_client_factory",
                        lambda **kw: _client(rec, **kw))
    r = _gen("--vendor", "fish", "--model", "s2.1-pro", "--default-voice", "delia",
             "--out", str(tmp_path))
    assert r.exit_code == cli_stimuli.EXIT_REFUSED, r.output
    assert "model: s2.1-pro" in r.output and "at $0.015/1k chars" in r.output
    assert "refusing" in r.output and rec.requests == []
    r = _gen("--vendor", "fish", "--usd-per-1k-chars", "0.5", "--out", str(tmp_path))
    assert "at $0.5/1k chars" in r.output and r.exit_code == cli_stimuli.EXIT_REFUSED


def test_cli_fish_generates_through_hook_then_caches(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(cli_stimuli, "fish_client_factory",
                        lambda **kw: _client(rec, **kw))
    args = ("--vendor", "fish", "--default-voice", "delia", "--out", str(tmp_path), "--yes",
            "--no-write-back")                     # never rewrite the checked-in fixtures
    r = _gen(*args)
    assert r.exit_code == 0, r.output
    n = len(list(tmp_path.glob("*.wav")))
    assert n > 0 and n == len(rec.requests) and f"wrote {n} clips" in r.output
    assert {json.loads(q.content)["reference_id"] for q in rec.requests} == {DELIA}
    assert all(q.headers["model"] == "drama-3" for q in rec.requests)
    r2 = _gen(*args)
    assert "0 to synthesize" in r2.output and len(rec.requests) == n


def test_cli_fish_blocks_before_any_request(tmp_path, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(cli_stimuli, "fish_client_factory",
                        lambda **kw: _client(rec, **kw))
    r = _gen("--vendor", "fish", "--out", str(tmp_path), "--yes")      # fixture voices unmapped
    assert r.exit_code == cli_stimuli.EXIT_BLOCKED and "nothing was sent" in r.output
    r = _gen("--vendor", "fish", "--model", "s9", "--out", str(tmp_path), "--yes")
    assert r.exit_code == cli_stimuli.EXIT_BLOCKED and "unknown fish model" in r.output
    assert rec.requests == []
    monkeypatch.setattr(cli_stimuli, "fish_client_factory", cli_stimuli._default_fish_factory)
    monkeypatch.delenv("FISH_API_KEY", raising=False)
    r = _gen("--vendor", "fish", "--default-voice", "delia", "--out", str(tmp_path), "--yes")
    assert r.exit_code == cli_stimuli.EXIT_BLOCKED and "set FISH_API_KEY" in r.output
    assert not any(tmp_path.iterdir())


def test_cli_fish_vendor_error_exits_3(tmp_path, monkeypatch):
    rec = Recorder(httpx.Response(402, text="payment required"))
    monkeypatch.setattr(cli_stimuli, "fish_client_factory",
                        lambda **kw: _client(rec, **kw))
    r = _gen("--vendor", "fish", "--default-voice", "delia", "--out", str(tmp_path), "--yes")
    assert r.exit_code == cli_stimuli.EXIT_TTS_ERROR and "402" in r.output
    assert KEY not in r.output and len(rec.requests) == 1


def test_cli_fake_vendor_still_works(tmp_path):
    r = _gen("--vendor", "fake", "--out", str(tmp_path), "--yes", "--no-write-back")
    assert r.exit_code == 0 and "wrote" in r.output
