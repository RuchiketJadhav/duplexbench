"""refasr-deepgram (decision B11): DeepgramASR against the fake server on 127.0.0.1 (no internet)."""
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from fake_deepgram_server import (FINAL_WORDS, FINALIZE_WORDS, KEY, MODEL_INFO,  # noqa: E402
                                  UUID, FakeDeepgramServer)

from duplexbench.channel import mulaw  # noqa: E402
from duplexbench.refasr.candidates import CandidateUnavailable  # noqa: E402
from duplexbench.refasr.candidates import deepgram as dg  # noqa: E402
from duplexbench.refasr.engine import Word  # noqa: E402

FRAME = np.zeros(160, np.int16)


@pytest.fixture
def srv():
    s = FakeDeepgramServer().start()
    yield s
    s.stop()


def _asr(server, **kw):
    kw.setdefault("finish_timeout_s", 3.0)
    return dg.DeepgramASR(api_key=KEY, url=server.url, **kw)


def _push_until(st, n_frames, want=0, wait_s=3.0):
    """Push n_frames of silence, then keep pushing (20 ms apart) until `want` words came."""
    got = []
    for _ in range(n_frames):
        got += st.push(FRAME)
    deadline = time.perf_counter() + wait_s
    while len(got) < want and time.perf_counter() < deadline:
        time.sleep(0.02)
        got += st.push(FRAME)
    return got


def _w(d):
    return Word(d["word"], round(d["start"] * 1_000), round(d["end"] * 1_000), d["confidence"])


def test_probe(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    assert dg.probe() == "set DEEPGRAM_API_KEY"
    monkeypatch.setenv("DEEPGRAM_API_KEY", "   ")
    assert dg.probe() == "set DEEPGRAM_API_KEY"
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k")
    assert dg.probe() is None
    monkeypatch.setattr(dg, "has_module", lambda m: False)
    assert dg.probe() == "pip install websockets"


def test_no_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(CandidateUnavailable, match="set DEEPGRAM_API_KEY"):
        dg.DeepgramASR()
    monkeypatch.setenv("MY_DG", "")
    with pytest.raises(CandidateUnavailable, match="set MY_DG"):
        dg.DeepgramASR(api_key_env="MY_DG")
    monkeypatch.setenv("MY_DG", "abc")
    a = dg.DeepgramASR(api_key_env="MY_DG")
    assert (a.name, a.streaming, a.word_timestamps) == ("deepgram", True, True)
    assert a.version == "nova-3/unpinned"
    assert dg.DeepgramASR(api_key="x", pinned_model_uuid="u1").version == "nova-3/u1"


def test_query_and_auth_header(srv):
    st = _asr(srv, utterance_end_ms=1_000, extra_params={"tag": "bench"}).stream()
    st.push(FRAME)
    st.finish()
    hdr = {k.lower(): v for k, v in srv.headers[0].items()}
    assert hdr["authorization"] == f"Token {KEY}"
    assert srv.paths[0].startswith("/v1/listen?")
    q = {k: v[0] for k, v in srv.queries[0].items()}
    assert q == {"model": "nova-3", "language": "en", "encoding": "linear16",
                 "sample_rate": "8000", "channels": "1", "interim_results": "true",
                 "endpointing": "100", "punctuate": "false", "smart_format": "false",
                 "numerals": "false", "utterance_end_ms": "1000", "tag": "bench"}


def test_only_final_results_release_words(srv):
    st = _asr(srv).stream()
    got = _push_until(st, 15)                # 15 frames = 4800 bytes: interim sent, no final
    time.sleep(0.2)
    got += st.push(FRAME)
    assert got == []                         # the interim word "helo" is never released
    got = _push_until(st, 15, want=2)        # past 9600 bytes: the final Results
    assert got == [_w(d) for d in FINAL_WORDS]
    assert got[0] == Word("hello", 100, 350, 0.98)          # ms ints, `word` not punctuated
    rest = st.finish()
    assert rest == [_w(d) for d in FINALIZE_WORDS]          # from_finalize words
    assert srv.texts[0] == [{"type": "Finalize"}, {"type": "CloseStream"}]
    assert all("helo" != w.text for w in got + rest)


def test_finish_returns_unreleased_words(srv):
    st = _asr(srv).stream()
    for _ in range(31):                       # final sent while we do not read it
        st.push(FRAME)
    words = st.finish()
    names = [w.text for w in words]
    assert names[-1] == "order" and set(names) <= {"hello", "there", "order"}
    assert st.finish() == []                  # idempotent
    with pytest.raises(RuntimeError, match="push after finish"):
        st.push(FRAME)


def test_transcribe_end_to_end_and_version(srv):
    a = _asr(srv)
    assert a.version == "nova-3/unpinned" and a.model_info is None
    words = a.transcribe(np.zeros(8_000, np.int16))         # 1 s, 50 frames
    assert [w.text for w in words] == ["hello", "there", "order"]
    assert a.version == f"nova-3/{UUID}" and a.model_uuid == UUID
    assert a.model_info == MODEL_INFO
    assert srv.audio_bytes[0] == 16_000 and srv.binary_frames[0] == 50


def test_never_pushed_stream_opens_no_connection(srv):
    assert _asr(srv).stream().finish() == []
    assert srv.headers == []


def test_pinned_uuid_mismatch_raises(srv):
    a = _asr(srv, pinned_model_uuid="uuid-other")
    st = a.stream()
    with pytest.raises(dg.RefASRVersionMismatch, match="uuid-other"):
        _push_until(st, 15, want=1)          # the interim Results carries the uuid
        st.finish()
    assert a.version == f"nova-3/{UUID}"     # records what the service reported


def test_pinned_uuid_match_runs(srv):
    a = _asr(srv, pinned_model_uuid=UUID)
    assert [w.text for w in a.transcribe(np.zeros(8_000, np.int16))] == \
        ["hello", "there", "order"]


def test_http_401_is_transport_error():
    with FakeDeepgramServer(key="another-key") as s:
        st = dg.DeepgramASR(api_key=KEY, url=s.url).stream()
        with pytest.raises(dg.RefASRTransportError, match="HTTP 401") as ei:
            st.push(FRAME)
        assert KEY not in str(ei.value) and s.rejected == 1


def test_connection_refused_is_transport_error():
    with FakeDeepgramServer() as s:
        url = s.url
    st = dg.DeepgramASR(api_key=KEY, url=url, connect_timeout_s=2.0).stream()
    with pytest.raises(dg.RefASRTransportError, match="deepgram connect") as ei:
        st.push(FRAME)
    assert KEY not in str(ei.value) and "\n" not in str(ei.value)


def test_finish_timeout_is_transport_error():
    with FakeDeepgramServer(silent_finish=True, final_at=10**9) as s:
        st = dg.DeepgramASR(api_key=KEY, url=s.url, finish_timeout_s=0.3).stream()
        st.push(FRAME)
        t0 = time.perf_counter()
        with pytest.raises(dg.RefASRTransportError, match="timeout"):
            st.finish()
        assert time.perf_counter() - t0 < 2.0


def test_non_json_message_is_transport_error():
    with FakeDeepgramServer(non_json=True) as s:
        st = dg.DeepgramASR(api_key=KEY, url=s.url).stream()
        with pytest.raises(dg.RefASRTransportError, match="non-JSON"):
            _push_until(st, 1, want=1)
            st.finish()


def test_server_close_is_transport_error():
    with FakeDeepgramServer(close_code=1011) as s:
        st = dg.DeepgramASR(api_key=KEY, url=s.url).stream()
        with pytest.raises(dg.RefASRTransportError, match="1011") as ei:
            _push_until(st, 1, want=1)
            st.finish()
        assert KEY not in str(ei.value)


def test_bytes_are_decoded_as_mulaw(srv):
    st = _asr(srv).stream()
    st.push(mulaw.encode(np.full(160, 1_000, np.int16)))   # 160 μ-law bytes
    st.push(FRAME)                                          # 160 int16 samples
    st.finish()
    assert st.sent_bytes == 2 * 320
    assert srv.audio_bytes[0] == 640 and srv.binary_frames[0] == 2


def test_price_and_projection():
    assert dg.usd_per_audio_min() == 0.0077
    assert dg.projected_usd(600.0) == pytest.approx(0.077)


def test_refasr_bench_prints_cost_and_skips_without_yes(tmp_path, monkeypatch):
    import soundfile as sf
    import typer
    from typer.testing import CliRunner

    from duplexbench import cli_refasr
    fx = tmp_path / "fm"
    fx.mkdir()
    sf.write(str(fx / "a.wav"), np.zeros(8_000 * 30, np.int16), 8_000, subtype="PCM_16")
    (fx / "a.txt").write_text("hello", encoding="utf-8")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "k")
    app = typer.Typer()

    @app.callback()
    def _cb() -> None:
        pass

    cli_refasr.register(app)
    out = tmp_path / "t.md"
    res = CliRunner().invoke(app, ["refasr-bench", "--out", str(out), "--fixtures", str(fx)])
    assert res.exit_code == 1 and not out.exists()
    assert "deepgram: paid API, projected 0.0039 USD for 30.0 s" in res.output
    assert "no paid API was called" in res.output


def test_refasr_bench_runs_deepgram_with_yes(tmp_path, monkeypatch, srv):
    import soundfile as sf
    import typer
    from typer.testing import CliRunner

    from duplexbench import cli_refasr
    from duplexbench.refasr import candidates_table as ct
    fx = tmp_path / "fm"
    fx.mkdir()
    sf.write(str(fx / "a.wav"), np.zeros(8_000, np.int16), 8_000, subtype="PCM_16")
    (fx / "a.txt").write_text("hello there order", encoding="utf-8")
    monkeypatch.setenv("DEEPGRAM_API_KEY", KEY)
    monkeypatch.setattr(ct, "probe", lambda c: None if c.name == "deepgram" else "blocked")
    orig = dg.DeepgramASR.__init__

    def init(self, *a, **kw):
        kw.setdefault("url", srv.url)
        orig(self, *a, **kw)
    monkeypatch.setattr(dg.DeepgramASR, "__init__", init)
    app = typer.Typer()

    @app.callback()
    def _cb() -> None:
        pass

    cli_refasr.register(app)
    out = tmp_path / "t.md"
    res = CliRunner().invoke(app, ["refasr-bench", "--yes", "--out", str(out),
                                   "--fixtures", str(fx)])
    assert res.exit_code == 0, res.output
    assert "(approved by --yes)" in res.output
    row = next(l for l in out.read_text(encoding="utf-8").splitlines()
               if l.startswith("| deepgram"))
    assert "0.0%" in row                      # WER 0 on "hello there order"
