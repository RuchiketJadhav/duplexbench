"""M2b-T02: pre-registration hashes; a threshold changed after preregister shows as a diff."""
import json
import shutil
from pathlib import Path

import pytest
import yaml

from duplexbench import preregister as pr
from duplexbench.thresholds import Thresholds

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def env(tmp_path, monkeypatch):
    systems = tmp_path / "systems"
    systems.mkdir()
    (systems / "mock.yaml").write_text("adapter: mock\nvoice: a\n", encoding="utf-8")
    lang = tmp_path / "langpacks" / "en-US"
    shutil.copytree(REPO / "configs" / "langpacks" / "en-US", lang)
    pack = tmp_path / "pack"
    shutil.copytree(FIX / "audio", pack / "audio")
    shutil.copy(FIX / "sched_001.yaml", pack)
    th = tmp_path / "thresholds.yaml"
    th.write_text(Thresholds().dump_yaml(), encoding="utf-8")
    priv = tmp_path / "private"
    (priv / "scenarios").mkdir(parents=True)
    (priv / "scenarios" / "p.yaml").write_text("id: p\n", encoding="utf-8")
    monkeypatch.setenv("DUPLEX_PRIVATE_DIR", str(priv))
    kw = dict(systems_dir=systems, pack_dir=pack, langpack_dir=lang, thresholds_path=th)
    return tmp_path, kw


def test_shape_and_record(env):
    tmp, kw = env
    h = pr.collect_hashes(**kw)
    assert set(h) == {"systems", "pack", "langpack", "thresholds", "private_tree", "harness_commit"}
    assert set(h["systems"]) == {"mock"} and len(h["systems"]["mock"]) == 16
    assert len(h["pack"]) == 64 and len(h["langpack"]) == 64 and len(h["private_tree"]) == 64 and len(h["thresholds"]) == 64
    assert h["harness_commit"] is None or len(h["harness_commit"]) == 40   # sha or null (no commit)
    path = pr.write_record(h, tmp / "results")
    assert path.name.startswith("preregister-") and path.suffix == ".json"
    rec = json.loads(path.read_text())
    assert rec["schema"] == 1 and rec["created_utc"].endswith("Z") and rec["hashes"] == h
    assert pr.check_against_latest(pr.collect_hashes(**kw), tmp / "results") == []


def test_threshold_change_after_preregister_is_a_diff(env):
    tmp, kw = env
    pr.write_record(pr.collect_hashes(**kw), tmp / "results")
    data = yaml.safe_load(kw["thresholds_path"].read_text())
    data["inactivity_run_ms"]["value"] += 20
    kw["thresholds_path"].write_text(yaml.safe_dump(data), encoding="utf-8")
    diffs = pr.check_against_latest(pr.collect_hashes(**kw), tmp / "results")
    assert len(diffs) == 1 and "thresholds:" in diffs[0]


def test_other_hashes_move(env, monkeypatch):
    tmp, kw = env
    base = pr.collect_hashes(**kw)
    (kw["systems_dir"] / "mock.yaml").write_text("adapter: mock\nvoice: b\n", encoding="utf-8")
    (Path(tmp) / "private" / "scenarios" / "p.yaml").write_text("id: q\n", encoding="utf-8")
    new = pr.collect_hashes(**kw)
    diffs = pr.diff_hashes(base, new)
    assert any(d.startswith("systems/mock:") for d in diffs)
    assert any(d.startswith("private_tree:") for d in diffs)
    monkeypatch.delenv("DUPLEX_PRIVATE_DIR")
    assert pr.collect_hashes(**kw)["private_tree"] == "unmounted"


def test_no_record_and_latest_wins(env):
    tmp, kw = env
    h = pr.collect_hashes(**kw)
    assert pr.check_against_latest(h, tmp / "results")[0].startswith("no pre-registration record")
    pr.write_record({**h, "thresholds": "old"}, tmp / "results")
    pr.write_record(h, tmp / "results")
    assert pr.check_against_latest(h, tmp / "results") == []


def test_missing_dirs_and_langpack_fallback(tmp_path):
    assert pr.pack_hash(tmp_path / "none") == "missing"
    assert pr.systems_hashes(tmp_path / "none") == {}
    assert pr.harness_commit(tmp_path) is None
