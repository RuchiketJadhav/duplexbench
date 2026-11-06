"""M3-T05 chaos: the provider socket drops mid-call -> transport_error, recording readable,
the next call runs. OpenAI Realtime adapter against tests/integration/fake_realtime_server.py
(no real provider, no OPENAI_API_KEY)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import soundfile as sf
import yaml
from typer.testing import CliRunner

from duplexbench.cli import app
from fake_realtime_server import USAGE, FakeRealtimeServer  # tests/integration is on sys.path

ROOT = Path(__file__).resolve().parents[2]


def _system(url: str) -> dict:
    raw = yaml.safe_load((ROOT / "configs" / "systems" / "openai_realtime.yaml").read_text())
    raw["params"] = dict(raw["params"], url=url, api_key_env="DUPLEXBENCH_FAKE_REALTIME_KEY")
    return raw


def test_drop_mid_call_then_next_call_runs(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DUPLEXBENCH_FAKE_REALTIME_KEY", "fake")
    with FakeRealtimeServer([{"drop_at_s": 1.4, "reply_ms": 1500}, {"reply_ms": 600}]) as srv:
        sysdir = tmp_path / "systems"
        sysdir.mkdir()
        (sysdir / "openai_realtime.yaml").write_text(yaml.safe_dump(_system(srv.url)))
        th = tmp_path / "thresholds.yaml"
        th.write_text("eend_silence_ms: 2500\n", encoding="utf-8")
        cfg = yaml.safe_load((ROOT / "configs" / "runs" / "smoke_mock.yaml").read_text())
        cfg.update(name="chaos", systems=["openai_realtime"], systems_dir=str(sysdir),
                   thresholds=str(th), results_dir=str(tmp_path / "results"), max_usd=1.0)
        p = tmp_path / "run.yaml"
        p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
        r = CliRunner().invoke(app, ["run", str(p), "--yes", "--allow-unregistered"])
        assert r.exit_code == 0, r.output
        assert srv.drops == 1 and len(srv.conns) == 2
        assert srv.headers[0].get("authorization") == "Bearer fake"
        assert "model=gpt-realtime-2.1" in srv.paths[0]
        su = srv.conns[1].received[0]
        assert su["type"] == "session.update"
        assert su["session"]["audio"]["input"]["format"] == {"type": "audio/pcmu"}
        assert srv.conns[1].appends > 50                     # caller audio streamed as mu-law
    db = next((tmp_path / "results").iterdir()) / "events.sqlite"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    calls = [dict(c) for c in conn.execute("SELECT * FROM calls ORDER BY call_id")]
    assert [c["outcome"] for c in calls] == ["transport_error", "completed"]
    dropped, ok = calls
    data, rate = sf.read(dropped["wav_path"], dtype="int16", always_2d=True)
    assert rate == 8000 and data.shape[1] == 2 and len(data) > 0
    flags = json.loads(dropped["flags_json"])
    assert flags["error"]["type"] == "transport" and flags.get("usd_lower_bound") is True
    ev = [r["kind"] for r in conn.execute(
        "SELECT kind FROM events WHERE call_id = ? ORDER BY t_ns, seq", (dropped["call_id"],))]
    assert "closed" in ev[ev.index("error"):]
    kinds = {r[0] for r in conn.execute("SELECT kind FROM events WHERE call_id = ?",
                                        (ok["call_id"],))}
    assert {"session_open", "first_audio_byte", "audio", "usage", "output_transcript"} <= kinds
    per_response = (100 * 32 + 20 * 4 + 170 * 64 + 10 * 24) / 1e6
    assert USAGE["input_token_details"]["audio_tokens"] == 100
    n = json.loads(ok["flags_json"])["usage_responses"]
    assert n >= 1 and ok["usd_actual"] == pytest.approx(per_response * n)
    data, _ = sf.read(ok["wav_path"], dtype="int16", always_2d=True)
    assert abs(data[:, 1]).max() > 0                         # agent tone reached the ear
    conn.close()
