"""M2-T01: scenario schema, validate() errors, pack hash, private-split refusal (spec §3, §11)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from duplexbench.scenarios.loader import PackError, load_pack, private_root, validate
from duplexbench.scenarios.schema import Scenario
from duplexbench.thresholds import Thresholds

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev"
TH = Thresholds()


def _scenario() -> Scenario:
    return Scenario.model_validate(yaml.safe_load((FIX / "sched_001.yaml").read_text()))


def _mut_overlay(s: Scenario, oid: str, **kw) -> Scenario:
    ovs = [o.model_copy(update=kw) if o.id == oid else o for o in s.overlays]
    return s.model_copy(update={"overlays": ovs})


def _mut_step(s: Scenario, sid: str, **kw) -> Scenario:
    steps = [st.model_copy(update=kw) if st.id == sid else st for st in s.steps]
    return s.model_copy(update={"steps": steps})


def _one_error(errs: list[str], needle: str) -> None:
    assert len(errs) == 1 and needle in errs[0], errs


def test_valid_fixture_loads():
    pack = load_pack(FIX)
    assert [s.id for s in pack.scenarios][0] == "sched_001"   # more fixtures sort after it
    assert len(pack.hash) == 64
    assert validate(pack.scenarios[0], th=TH) == []


def test_hash_is_stable():
    assert load_pack(FIX).hash == load_pack(FIX).hash


def test_schema_rejects_unknown_class_and_extra_field():
    data = yaml.safe_load((FIX / "sched_001.yaml").read_text())
    data["overlays"][0]["cls"] = "X-NEW"
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)
    data = yaml.safe_load((FIX / "sched_001.yaml").read_text())
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        Scenario.model_validate(data)


# -- each validate() error from a one-field mutation ------------------------------
def test_anchor_not_a_step():
    _one_error(validate(_mut_overlay(_scenario(), "o1", anchor_step="s9"), th=TH),
               "is not a step")


def test_yint_too_few_words():
    _one_error(validate(_mut_overlay(_scenario(), "o1", text="Friday instead"), th=TH),
               ">= 3 words")


def test_yint_without_response_anchors():
    _one_error(validate(_mut_overlay(_scenario(), "o1", response_anchors=[]), th=TH),
               "response_anchors")


def test_hbc_token_not_in_list():
    _one_error(validate(_mut_overlay(_scenario(), "o2", text="absolutely"), th=TH),
               "not in the langpack list")


def test_hbc_token_list_is_injectable():
    s = _mut_overlay(_scenario(), "o2", text="absolutely")
    assert validate(s, th=TH, hbc_tokens=["absolutely"]) == []


def test_ppause_out_of_range():
    _one_error(validate(_mut_step(_scenario(), "s1", pause_ms=1600), th=TH), "outside [600,1500]")
    _one_error(validate(_mut_step(_scenario(), "s1", pause_ms=500), th=TH), "outside [600,1500]")


def test_in_pause_on_step_without_pause():
    # o3 (P-PAUSE, in_pause) sits on s1; drop s1's pause -> in_pause error + P-PAUSE range error,
    # so mutate a different overlay instead: move the H-BC onto s2 (no pause) as in_pause.
    errs = validate(_mut_overlay(_scenario(), "o2", anchor_step="s2", placement="in_pause"), th=TH)
    _one_error(errs, "without pause_ms")


def test_eend_without_enough_silence():
    _one_error(validate(_mut_overlay(_scenario(), "o4", resumption_ms=5999), th=TH),
               "E-END needs")
    _one_error(validate(_mut_overlay(_scenario(), "o4", resumption_ms=None), th=TH),
               "E-END needs")


def test_private_split_inside_repo(tmp_path):
    s = _scenario().model_copy(update={"split": "private-test"})
    _one_error(validate(s, th=TH, path=tmp_path / "x.yaml", repo_root=tmp_path),
               "inside the repo")
    assert validate(s, th=TH, path=Path("/elsewhere/x.yaml"), repo_root=tmp_path) == []


# -- pack hash and private refusal --------------------------------------------------
def _copy_pack(dst: Path) -> Path:
    shutil.copytree(FIX, dst)
    return dst


def test_hash_changes_when_clip_byte_changes(tmp_path):
    pack_dir = _copy_pack(tmp_path / "pack")
    h0 = load_pack(pack_dir).hash
    wav = pack_dir / "audio" / "silence_100ms.wav"
    b = bytearray(wav.read_bytes())
    b[-1] ^= 0x01
    wav.write_bytes(bytes(b))
    assert load_pack(pack_dir).hash != h0


def test_hash_changes_when_scenario_changes(tmp_path):
    pack_dir = _copy_pack(tmp_path / "pack")
    h0 = load_pack(pack_dir).hash
    y = pack_dir / "sched_001.yaml"
    y.write_text(y.read_text().replace('version: "1"', 'version: "2"'))
    assert load_pack(pack_dir).hash != h0


def test_missing_clip_refused(tmp_path):
    pack_dir = _copy_pack(tmp_path / "pack")
    (pack_dir / "audio" / "silence_100ms.wav").unlink()
    with pytest.raises(PackError, match="clip not found"):
        load_pack(pack_dir)


def test_load_pack_refuses_private_under_repo_root(tmp_path):
    repo = tmp_path / "repo"
    pack_dir = _copy_pack(repo / "scenarios" / "leak")
    y = pack_dir / "sched_001.yaml"
    y.write_text(y.read_text().replace("split: public-dev", "split: private-test"))
    with pytest.raises(PackError, match="inside the repo"):
        load_pack(pack_dir, repo_root=repo)
    # the same pack outside the repo root (a mounted private dir) loads
    load_pack(pack_dir, repo_root=tmp_path / "other-repo")


def test_private_root_from_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DUPLEX_PRIVATE_DIR", raising=False)
    assert private_root() is None
    monkeypatch.setenv("DUPLEX_PRIVATE_DIR", str(tmp_path))
    assert private_root() == tmp_path.resolve()


def test_tooldef_anchor_step_optional_and_validated():
    """ToolDef.anchor_step (metrics/task.py t_tool_offset): optional, must name a step."""
    from duplexbench.scenarios.schema import ToolDef
    assert ToolDef(name="lookup", kind="read").anchor_step is None
    assert ToolDef(name="lookup", kind="read", anchor_step="s1").anchor_step == "s1"
    c = Scenario.model_validate(yaml.safe_load((FIX / "track_c_001.yaml").read_text()))
    ok = c.model_copy(update={"tools": [c.tools[0].model_copy(update={"anchor_step": "s2"}),
                                        *c.tools[1:]]})
    assert validate(ok, th=TH) == []
    bad = c.model_copy(update={"tools": [c.tools[0].model_copy(update={"anchor_step": "nope"}),
                                         *c.tools[1:]]})
    _one_error(validate(bad, th=TH), "anchor_step 'nope' is not a step")
