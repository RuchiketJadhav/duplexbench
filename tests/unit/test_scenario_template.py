"""M2b-T01: the template validates once its required fields are filled; `scenario validate` prints a table."""
import shutil
from pathlib import Path

import typer
import yaml
from typer.testing import CliRunner

from duplexbench.cli_tools import register
from duplexbench.scenarios.schema import Scenario
from duplexbench.scenarios.template import render_template, todo_fields, validate_dir, validate_file
from duplexbench.thresholds import Thresholds

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev"
FILL = {"id": "sched_a_001", "caller_voice": "voice-a"}


def _app():
    app = typer.Typer()

    @app.callback()
    def _cb() -> None:
        pass

    register(app)
    return app


def test_template_has_every_field_and_comments():
    text = render_template("scheduling", "A", "public-dev")
    data = yaml.safe_load(text)
    assert set(data) == set(Scenario.model_fields)
    step_fields = {"id", "text", "clip", "speech_onset_ms", "speech_end_ms", "pause_ms", "resumption_text"}
    assert set(data["steps"][0]) == step_fields
    for f in ("cls:", "anchor_step:", "placement:", "offset_ms:", "response_anchors:", "resumption_ms:",
              "voice_id:", "result_anchors:", "latency_ms:"):
        assert f in text, f
    body = [l for l in text.splitlines() if l and not l.lstrip().startswith("#")]
    assert all("#" in l for l in body), [l for l in body if "#" not in l]
    th = Thresholds()
    assert f"[{th.pause_ms[0]}, {th.pause_ms[1]}]" in text


def test_filled_template_validates(tmp_path):
    data = yaml.safe_load(render_template("scheduling", "A", "public-dev"))
    assert set(todo_fields(data)) == {"id", "caller_voice", "steps.0.text"}
    p = tmp_path / "t.yaml"
    p.write_text(render_template("scheduling", "A", "public-dev"), encoding="utf-8")
    assert not validate_file(p).ok
    data.update(FILL)
    data["steps"][0]["text"] = "I need to book a cleaning."
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    r = validate_file(p)
    assert r.ok, r.problems
    assert r.scenario_id == "sched_a_001"


def test_cli_new_and_validate(tmp_path):
    runner = CliRunner()
    out = tmp_path / "pack" / "n.yaml"
    r = runner.invoke(_app(), ["scenario", "new", "--domain", "billing", "--track", "C",
                               "--split", "public-dev", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert yaml.safe_load(out.read_text())["source"] == "simulated"
    r = runner.invoke(_app(), ["scenario", "new", "--domain", "billing", "--track", "C",
                               "--out", str(out)])
    assert r.exit_code == 1                                   # exists, no --force
    r = runner.invoke(_app(), ["scenario", "validate", str(tmp_path / "pack")])
    assert r.exit_code == 1 and "FAIL" in r.output and "still TODO" in r.output
    good = tmp_path / "good"
    shutil.copytree(FIX / "audio", good / "audio")
    shutil.copy(FIX / "sched_001.yaml", good)
    r = runner.invoke(_app(), ["scenario", "validate", str(good)])
    assert r.exit_code == 0, r.output
    assert "sched_001" in r.output and "1 file(s), 0 failing" in r.output   # only sched_001 copied


def test_validate_dir_reports_semantic_errors(tmp_path):
    data = yaml.safe_load((FIX / "sched_001.yaml").read_text())
    data["overlays"][0]["response_anchors"] = []
    (tmp_path / "audio").mkdir()
    shutil.copy(FIX / "audio" / "silence_100ms.wav", tmp_path / "audio")
    (tmp_path / "bad.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    [res] = validate_dir(tmp_path)
    assert any("response_anchors" in p for p in res.problems)


def test_private_template_refused_inside_repo(monkeypatch):
    monkeypatch.delenv("DUPLEX_PRIVATE_DIR", raising=False)
    r = CliRunner().invoke(_app(), ["scenario", "new", "--domain", "x", "--track", "A",
                                    "--split", "private-test"])
    assert r.exit_code == 2
    repo = Path(__file__).resolve().parents[2]
    r = CliRunner().invoke(_app(), ["scenario", "new", "--domain", "x", "--track", "A",
                                    "--split", "private-test", "--out", str(repo / "scratch_x.yaml")])
    assert r.exit_code == 2 and not (repo / "scratch_x.yaml").exists()
