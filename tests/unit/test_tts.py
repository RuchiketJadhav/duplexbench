"""M2-T04 (code only): TTS cache, word timestamps, cost projection, approve-first CLI."""
from pathlib import Path

import numpy as np
import pytest
import typer
from typer.testing import CliRunner

from duplexbench import cli_stimuli
from duplexbench.stimuli.fake_tts import FakeTTS
from duplexbench.stimuli.tts import (TTSClient, Word, cache_key, pack_items, project_usd,
                                     synthesize)

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev"


def test_fake_is_a_client_and_words_are_proportional():
    c = FakeTTS()
    assert isinstance(c, TTSClient)
    pcm, sr, words = c.synthesize("hi there everybody", "v", 1.0)
    assert sr == 24000 and pcm.dtype == np.int16
    assert [w.text for w in words] == ["hi", "there", "everybody"]
    durs = [w.end_ms - w.start_ms for w in words]
    assert durs[1] / durs[0] == pytest.approx(5 / 2, rel=0.02)
    assert words[-1].end_ms == pytest.approx(len(pcm) * 1000 / sr, abs=1)
    _, _, slow = c.synthesize("hi there everybody", "v", 0.5)
    assert slow[-1].end_ms == pytest.approx(2 * words[-1].end_ms, abs=3)


def test_cache_hit_skips_client_and_keeps_words(tmp_path):
    c = FakeTTS()
    a = synthesize("hold on a second", "voice-a", 1.0, c, cache_dir=tmp_path)
    b = synthesize("hold on a second", "voice-a", 1.0, c, cache_dir=tmp_path)
    assert c.calls == 1
    assert np.array_equal(a[0], b[0]) and a[1] == b[1] and a[2] == b[2]
    assert all(isinstance(w, Word) for w in b[2])
    key = cache_key("fake", "voice-a", 1.0, "hold on a second")
    assert (tmp_path / f"{key}.wav").is_file() and (tmp_path / f"{key}.json").is_file()
    synthesize("hold on a second", "voice-b", 1.0, c, cache_dir=tmp_path)    # voice in key
    synthesize("hold on a second", "voice-a", 1.1, c, cache_dir=tmp_path)    # rate in key
    assert c.calls == 3


def test_cache_key_covers_vendor():
    assert cache_key("fake", "v", 1.0, "x") != cache_key("other", "v", 1.0, "x")
    assert cache_key("fake", "v", 1, "x") == cache_key("fake", "v", 1.0, "x")


def test_project_usd():
    assert project_usd(["a" * 600, "b" * 400], 0.03) == pytest.approx(0.03)
    assert project_usd([], 1.0) == 0.0


def test_pack_items_dedupes_and_skips_empty():
    from duplexbench.scenarios.loader import load_pack
    items = pack_items(load_pack(FIX).scenarios)
    assert items and all(i.text.strip() for i in items)
    assert len({(i.voice, i.rate, i.text) for i in items}) == len(items)


def _app() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def _m() -> None: ...

    cli_stimuli.register(app)
    return app


def test_cli_refuses_without_yes(tmp_path):
    r = CliRunner().invoke(_app(), ["stimuli", "generate", str(FIX), "--vendor", "acme",
                                    "--usd-per-1k-chars", "0.03", "--out", str(tmp_path)])
    assert r.exit_code == cli_stimuli.EXIT_REFUSED, r.output
    assert "projected cost: $" in r.output and "refusing" in r.output
    assert not any(tmp_path.iterdir())


def test_cli_real_vendor_blocked_even_with_yes(tmp_path):
    r = CliRunner().invoke(_app(), ["stimuli", "generate", str(FIX), "--vendor", "acme",
                                    "--usd-per-1k-chars", "0.03", "--out", str(tmp_path), "--yes"])
    assert r.exit_code == cli_stimuli.EXIT_BLOCKED and "nothing was sent" in r.output
    r = CliRunner().invoke(_app(), ["stimuli", "generate", str(FIX), "--vendor", "acme",
                                    "--out", str(tmp_path), "--yes"])
    assert r.exit_code == cli_stimuli.EXIT_BLOCKED and "price unknown" in r.output


def test_cli_fake_vendor_generates_then_caches(tmp_path):
    args = ["stimuli", "generate", str(FIX), "--vendor", "fake", "--out", str(tmp_path), "--yes"]
    r = CliRunner().invoke(_app(), args)
    assert r.exit_code == 0, r.output
    n = len(list(tmp_path.glob("*.wav")))
    assert n > 0 and f"wrote {n} clips" in r.output
    r2 = CliRunner().invoke(_app(), args)
    assert "0 to synthesize" in r2.output and "characters: 0" in r2.output
