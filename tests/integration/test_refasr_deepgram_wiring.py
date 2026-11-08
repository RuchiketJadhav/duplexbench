"""refasr-deepgram wiring: calibrate.refasr_stage and cli_run.build_refasr through DeepgramASR
against the fake server on 127.0.0.1 (no internet)."""
import numpy as np
import pytest
from fake_deepgram_server import KEY, UUID, FakeDeepgramServer  # tests/integration is on sys.path

from duplexbench.calibrate import refasr_stage
from duplexbench.clock import RealClock
from duplexbench.refasr.candidates import CandidateUnavailable
from duplexbench.refasr.candidates.deepgram import DeepgramASR, RefASRVersionMismatch
from duplexbench.thresholds import Thresholds


def test_refasr_stage_through_deepgram_stream():
    with FakeDeepgramServer() as s:
        asr = DeepgramASR(api_key=KEY, url=s.url)
        res = refasr_stage(asr, np.zeros(8_000, np.int16), Thresholds(), RealClock())
    # paced at real time: the final Results (sent at 0.3 s of audio) comes back during
    # push(), so "hello"/"there" (end 350/620 ms) are measured as partials, not at finish
    assert res["n_words"] == 3
    assert res["partial_p90_ms"] is not None and res["partial_p90_ms"] < 1_000
    assert res["version"] == f"deepgram:nova-3/{UUID}"
    assert s.audio_bytes[0] == 16_000


def test_build_refasr_deepgram(monkeypatch):
    from duplexbench.cli_run import build_refasr, refasr_id
    monkeypatch.setenv("DEEPGRAM_API_KEY", KEY)
    eng = build_refasr({"refasr": {"candidate": "deepgram",
                                   "kwargs": {"endpointing": 25, "pinned_model_uuid": UUID}}})
    assert isinstance(eng, DeepgramASR) and eng.endpointing == 25
    assert refasr_id(eng) == f"deepgram:nova-3/{UUID}"
    eng = build_refasr({"refasr": {"candidate": "deepgram", "kwargs": {}}})
    assert refasr_id(eng) == "deepgram:nova-3/unpinned"


def test_build_refasr_deepgram_without_key(monkeypatch):
    from duplexbench.cli_run import RunRefused, RunConfig, _resolve_refasr, build_refasr
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    with pytest.raises(CandidateUnavailable, match="set DEEPGRAM_API_KEY"):
        build_refasr({"refasr": {"candidate": "deepgram"}})
    cfg = RunConfig.model_construct(simulator={"refasr": {"candidate": "deepgram"}})
    with pytest.raises(RunRefused, match="simulator.refasr: set DEEPGRAM_API_KEY"):
        _resolve_refasr(cfg)


def test_version_mismatch_is_detected_for_the_run_stop():
    from duplexbench.cli_run import _is_version_mismatch
    assert _is_version_mismatch(RefASRVersionMismatch("x"))
    assert not _is_version_mismatch(RuntimeError("x")) and not _is_version_mismatch(None)


def test_calibrate_cli_refasr_needs_yes(tmp_path, monkeypatch):
    import typer
    from typer.testing import CliRunner

    from duplexbench import cli_calibrate
    monkeypatch.setenv("DEEPGRAM_API_KEY", KEY)
    rc = tmp_path / "run.yaml"
    rc.write_text("simulator:\n  refasr: {candidate: deepgram, kwargs: {}}\n", encoding="utf-8")
    app = typer.Typer()

    @app.callback()
    def _cb() -> None:
        pass

    cli_calibrate.register(app)
    res = CliRunner().invoke(app, ["calibrate", "--refasr-config", str(rc)])
    assert res.exit_code == 1
    # fix-41: the default clip is 195 s, and the projection follows the clip's duration
    assert "projected 0.0250 USD for 195.0 s" in res.output and "No paid API" in res.output
    from duplexbench import calibrate as cal
    monkeypatch.setattr(cal, "DEFAULT_REFASR_CLIP",
                        cal.ROOT / "tests" / "fixtures" / "refasr_calib_speech_60s.wav")
    res = CliRunner().invoke(app, ["calibrate", "--refasr-config", str(rc)])
    assert res.exit_code == 1 and "projected 0.0077 USD for 60.0 s" in res.output
