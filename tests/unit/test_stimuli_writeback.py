"""fix-31: `stimuli generate` writes clips, onset/end and resumption clips back into the YAML."""
import shutil
from pathlib import Path

import pytest
import typer
import yaml
from typer.testing import CliRunner

from duplexbench import cli_stimuli
from duplexbench.scenarios.loader import clip_sources, load_pack, validate
from duplexbench.scenarios.schema import Scenario
from duplexbench.stimuli.writeback import plan_pack, rewrite_yaml

DRAFTS = Path(__file__).resolve().parents[2] / "scenarios" / "public-dev"
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev"


def _app() -> typer.Typer:
    app = typer.Typer()

    @app.callback()
    def _m() -> None: ...

    cli_stimuli.register(app)
    return app


def _gen(pack: Path, *extra):
    return CliRunner().invoke(_app(), ["stimuli", "generate", str(pack), "--vendor", "fake",
                                       "--yes", *extra])


def _fresh_drafts(d: Path) -> Path:
    """Byte copies of the five drafts with every clip / onset / end reset to null (the
    checked-in drafts may already be generated), so the tests never depend on
    scenarios/public-dev/audio existing."""
    d.mkdir()
    for f in DRAFTS.glob("*.yaml"):
        shutil.copyfile(f, d / f.name)          # byte copy: line endings kept
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        if data.get("source") != "scripted":
            continue
        rewrite_yaml(d / f.name,
                     {st["id"]: {"clip": None, "speech_onset_ms": None, "speech_end_ms": None}
                      for st in data["steps"]},
                     {o["id"]: {"clip": None} for o in data.get("overlays") or []})
    return d


@pytest.fixture
def pack(tmp_path) -> Path:
    return _fresh_drafts(tmp_path / "pd")


def _snapshot(d: Path) -> dict[str, bytes]:
    return {p.relative_to(d).as_posix(): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def test_drafts_carry_resumption_text_in_the_schema_field(pack):
    p = load_pack(pack)
    ycmd = [o for s in p.scenarios for o in s.overlays if o.cls == "Y-CMD"]
    assert ycmd and all(o.resumption_text for o in ycmd)
    assert "Resumption line" not in (DRAFTS / "pd_a_001.yaml").read_text(encoding="utf-8")


def test_generate_writes_back_and_pack_is_runnable(pack):
    from duplexbench import cli_run
    before = load_pack(pack).hash
    r = _gen(pack)
    assert r.exit_code == 0, r.output
    assert "pack hash changed" in r.output
    p = load_pack(pack)
    assert p.hash != before
    for s in p.scenarios:
        if s.source != "scripted":
            # simulated callers need no clips: their YAML is untouched
            assert (pack / f"{s.id}.yaml").read_bytes() == (DRAFTS / f"{s.id}.yaml").read_bytes()
            assert all(st.clip is None for st in s.steps)
            continue
        for st in s.steps:
            assert st.clip and st.clip.startswith("audio/tts/") and st.clip.endswith(".wav")
            assert st.speech_onset_ms is not None and st.speech_end_ms is not None
            assert 0 <= st.speech_onset_ms < st.speech_end_ms
        for o in s.overlays:
            if o.text.strip():
                assert o.clip and o.clip.startswith("audio/tts/")
            else:
                assert o.clip is None                   # P-PAUSE, E-END
        base = p.paths[s.id].parent
        req, opt = clip_sources(s, base)
        assert all(q.is_file() for q in req.values())
        for o in s.overlays:
            if o.cls == "Y-CMD":
                res = opt[f"{o.id}/resumption"]
                assert res.is_file() and res.with_suffix(".json").is_file()
                assert res.name == Path(o.clip).stem + "_resumption.wav"
        clips = cli_run.load_clips(s, p.paths[s.id], 8000)
        assert set(req) <= set(clips)
        assert all(f"{o.id}/resumption" in clips for o in s.overlays if o.cls == "Y-CMD")


def test_generate_keeps_comments_and_header(pack):
    src = (pack / "pd_a_001.yaml").read_text(encoding="utf-8")
    _gen(pack)
    txt = (pack / "pd_a_001.yaml").read_text(encoding="utf-8")
    assert txt.splitlines()[:2] == src.splitlines()[:2]
    assert "# P-PAUSE is always in_pause" in txt
    assert txt.count("\n") == src.count("\n")          # every field existed: edits in place


def test_second_run_is_a_no_op(pack):
    assert _gen(pack).exit_code == 0
    snap = _snapshot(pack)
    r = _gen(pack)
    assert r.exit_code == 0 and "0 to synthesize" in r.output and "nothing changed" in r.output
    assert _snapshot(pack) == snap


def test_no_write_back_leaves_yaml_unchanged(pack):
    yamls = {f.name: f.read_bytes() for f in pack.glob("*.yaml")}
    r = _gen(pack, "--no-write-back")
    assert r.exit_code == 0, r.output
    assert {f.name: f.read_bytes() for f in pack.glob("*.yaml")} == yamls
    assert not list((pack / "audio" / "tts").glob("*_resumption.wav"))


def test_resumption_lines_count_toward_projection(pack):
    r = CliRunner().invoke(_app(), ["stimuli", "generate", str(pack), "--vendor", "fake"])
    assert r.exit_code == cli_stimuli.EXIT_REFUSED
    chars = int(next(l for l in r.output.splitlines() if l.startswith("characters:")).split()[1])
    p = load_pack(pack)
    ycmd_res = sum(len(o.resumption_text) for s in p.scenarios for o in s.overlays
                   if o.resumption_text)
    assert ycmd_res > 0
    from duplexbench.stimuli.tts import pack_items
    assert chars == sum(len(i.text) for i in pack_items(p.scenarios))
    assert any(i.text == "Sorry about that. Okay, go ahead." for i in pack_items(p.scenarios))


def test_existing_non_tts_clips_are_kept(tmp_path):
    d = tmp_path / "fx"
    shutil.copytree(FIX, d)
    r = _gen(d)
    assert r.exit_code == 0, r.output
    data = yaml.safe_load((d / "track_a_002.yaml").read_text(encoding="utf-8"))
    assert data["steps"][0]["clip"] == "audio/a002_s1.wav"     # imported clip kept
    sched = yaml.safe_load((d / "sched_001.yaml").read_text(encoding="utf-8"))
    o1 = next(o for o in sched["overlays"] if o["id"] == "o1")
    assert o1["clip"].startswith("audio/tts/")                # was unset: filled
    assert sched["steps"][0]["clip"] == "audio/silence_100ms.wav"
    load_pack(d, check_tools=False)      # fixture track_c_001 has scoring-only tool names


def test_conflicting_resumptions_stop_before_synthesis(pack):
    f = pack / "pd_a_002.yaml"
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    data["id"] = "pd_a_002b"
    data["overlays"][3]["resumption_text"] = "A different line."
    data["caller_voice"] = "voice-f1"
    data["speaking_rate"] = 1.0
    data["overlays"][3]["text"] = "hold on"                   # same clip as pd_a_001/o4
    (pack / "pd_a_002b.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    r = _gen(pack)
    assert r.exit_code == cli_stimuli.EXIT_BLOCKED, r.output
    assert "write-back conflict" in r.output
    assert not (pack / "audio").exists()


def test_resumption_text_needs_ycmd_with_resumption_ms(pack):
    s = Scenario.model_validate(yaml.safe_load((pack / "pd_a_001.yaml").read_text("utf-8")))
    hbc = s.overlays[0].model_copy(update={"resumption_text": "back"})
    bad = s.model_copy(update={"overlays": [hbc, *s.overlays[1:]]})
    assert any("resumption_text needs a Y-CMD" in e for e in validate(bad))
    assert validate(s) == []


def test_pack_hash_unchanged_for_packs_without_resumption_text():
    # the fixture pack does not use the new field; its hash must not depend on it existing
    import hashlib
    from duplexbench.storage import canonical_json
    p = load_pack(FIX, check_tools=False)
    h = hashlib.sha256()
    dumps = []
    for s in p.scenarios:
        d = s.model_dump(mode="json")
        if not d.get("seed"):
            d.pop("seed", None)           # G2/G4 scenario seed: an empty one is left out too
        for o in d["overlays"]:
            del o["resumption_text"]
        dumps.append(d)
    h.update(canonical_json(dumps).encode())
    from duplexbench.scenarios.loader import _clip_paths, _sha256_file
    for s in p.scenarios:
        for name, f in sorted(_clip_paths(s, p.paths[s.id].parent)):
            h.update(f"\n{s.id}:{name}:{_sha256_file(f)}".encode())
    assert p.hash == h.hexdigest()


def test_rewrite_yaml_inserts_missing_fields_and_keeps_crlf(tmp_path):
    f = tmp_path / "s.yaml"
    f.write_bytes(b"# head\r\nsteps:\r\n  - id: s1\r\n    text: \"hi there\"  # c\r\n"
                  b"    pause_ms: 900\r\noverlays: []\r\n")
    how = rewrite_yaml(f, {"s1": {"clip": "audio/tts/x.wav", "speech_onset_ms": 0,
                                  "speech_end_ms": 120}}, {})
    assert how == "edited"
    raw = f.read_bytes()
    assert b"\n" not in raw.replace(b"\r\n", b"")
    d = yaml.safe_load(raw.decode())
    assert d["steps"][0] == {"id": "s1", "text": "hi there", "clip": "audio/tts/x.wav",
                             "speech_onset_ms": 0, "speech_end_ms": 120, "pause_ms": 900}
    assert raw.startswith(b"# head\r\n") and b"# c" in raw
    assert rewrite_yaml(f, {"s1": {"clip": "audio/tts/x.wav"}}, {}) == "unchanged"


def test_plan_skips_simulated_scenarios(pack):
    plan = plan_pack(load_pack(pack), client_id="fake", out_dir=pack / "audio" / "tts")
    assert {sp.scenario_id for sp in plan.scenarios} == {"pd_a_001", "pd_a_002", "pd_d_001"}
    assert plan.clashes == []
