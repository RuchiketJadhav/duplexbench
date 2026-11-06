"""M4a-T02: reference-ASR candidate wrappers, table and the approval-gated CLI."""
import importlib
import sys

import numpy as np
import pytest
import typer
from typer.testing import CliRunner

from duplexbench.refasr import candidates_table as ct
from duplexbench.refasr.candidates import BufferedStreamer, CandidateUnavailable, require, upsample
from duplexbench.refasr.engine import Word

WRAPPERS = [("faster_whisper", "FasterWhisperASR", "faster_whisper"),
            ("parakeet", "ParakeetASR", "nemo"),
            ("parakeet", "NemotronStreamingASR", "nemo"),
            ("kyutai", "KyutaiASR", "moshi"),
            ("moonshine", "MoonshineASR", "moonshine_onnx")]


@pytest.mark.parametrize("module,cls,dep", WRAPPERS)
def test_unavailable_raises_with_pip_hint(monkeypatch, module, cls, dep):
    monkeypatch.setitem(sys.modules, dep, None)          # import of `dep` now fails
    mod = importlib.import_module(f"duplexbench.refasr.candidates.{module}")
    with pytest.raises(CandidateUnavailable, match="pip install"):
        getattr(mod, cls)()


def test_require_and_probe():
    with pytest.raises(CandidateUnavailable, match="pip install nothing-here"):
        require("duplexbench_no_such_module", "nothing-here")
    for c in ct.CANDIDATES:
        r = ct.probe(c)
        assert r is None or "pip install" in r


def test_upsample_and_buffered_streamer():
    x = (np.sin(np.arange(800) / 7) * 8000).astype(np.int16)
    y = upsample(x, 16000)
    assert y.dtype == np.float32 and len(y) == 1600 and np.abs(y).max() <= 1.0
    got = []
    s = BufferedStreamer(lambda pcm: got.append(len(pcm)) or [Word("hi", 0, 10)])
    assert s.push(x[:160]) == [] and s.push(bytes([0xff]) * 160) == []
    assert s.finish() == [Word("hi", 0, 10)] and got == [320]


def test_table_renders_every_column_and_candidate():
    md = ct.render(availability={c.name: "pip install x" for c in ct.CANDIDATES})
    header = next(l for l in md.splitlines() if l.startswith("| candidate"))
    assert [h.strip() for h in header.strip("|").split("|")] == list(ct.COLUMNS)
    body = [l for l in md.splitlines() if l.startswith("| ") and not l.startswith("| candidate")]
    assert len(body) == len(ct.CANDIDATES)
    for line in body:
        assert len(line.strip("|").split(" | ")) == len(ct.COLUMNS)
        assert line.count(ct.UNMEASURED) == 3
    assert md.count("blocked: pip install x") == len(ct.CANDIDATES)
    assert "human picks" in md.lower()


def test_table_fills_measurements(tmp_path):
    p = ct.write(tmp_path / "t.md", {"moonshine": {"wer": 0.125, "rtf": 0.4}},
                 {c.name: None for c in ct.CANDIDATES})
    md = p.read_text(encoding="utf-8")
    row = next(l for l in md.splitlines() if l.startswith("| moonshine"))
    assert "12.5%" in row and "0.40" in row and row.count(ct.UNMEASURED) == 1


def test_cli_refuses_without_yes(tmp_path):
    from duplexbench import cli_refasr
    app = typer.Typer()

    @app.callback()
    def _cb() -> None:
        pass

    cli_refasr.register(app)
    out = tmp_path / "t.md"
    res = CliRunner().invoke(app, ["refasr-bench", "--out", str(out)])
    assert res.exit_code == 1
    assert "approval required" in res.output and "Nothing was downloaded" in res.output
    assert not out.exists()


def test_cli_with_yes_writes_table_for_blocked_candidates(tmp_path, monkeypatch):
    from duplexbench import cli_refasr
    monkeypatch.setattr(ct, "probe", lambda c: "pip install blocked")
    app = typer.Typer()

    @app.callback()
    def _cb() -> None:
        pass

    cli_refasr.register(app)
    out = tmp_path / "t.md"
    res = CliRunner().invoke(app, ["refasr-bench", "--yes", "--out", str(out)])
    assert res.exit_code == 0, res.output
    assert out.read_text(encoding="utf-8").count("blocked: pip install blocked") == 5


def test_wer_hand_computed():
    from duplexbench.cli_refasr import wer
    assert wer("my order is late", "my order was late") == (1, 4)
    assert wer("a b c", "a c") == (1, 3)
    assert wer("Hello, there!", "hello there") == (0, 2)
