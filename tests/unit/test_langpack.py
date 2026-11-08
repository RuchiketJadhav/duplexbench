"""M2-T02: language pack (spec §3.2 lists, §11 private extensions)."""
import re
from pathlib import Path

import pytest

from duplexbench.metrics import normalize as nz
from duplexbench.scenarios.loader import SPEC_HBC_TOKENS
from duplexbench.stimuli.langpack import LangPack, LangPackError

ROOT = Path(__file__).resolve().parents[2]
PUB = ROOT / "configs" / "langpacks" / "en-US"

SPEC_YCMD = ("no", "stop", "wait", "hold on", "hang on", "one second", "sorry?")
SPEC_WAIT = ("wait", "hold on", "hang on", "one second")
SPEC_HBC = ("mm-hm", "uh-huh", "yeah", "right", "okay", "got it", "sure")


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("DUPLEX_PRIVATE_DIR", raising=False)


def test_lists_match_spec():
    lp = LangPack.load(PUB)
    assert lp.lang == "en-US"
    assert lp.ycmd_tokens == SPEC_YCMD
    assert lp.wait_class == SPEC_WAIT
    assert set(lp.wait_class) <= set(lp.ycmd_tokens)
    assert lp.hbc_tokens == SPEC_HBC
    assert lp.hbc_tokens == SPEC_HBC_TOKENS        # loader default cannot drift from the pack
    assert lp.private_dir is None


def test_patterns_public_starter_sets():
    lp = LangPack.load(PUB)
    assert len(lp.success_patterns) >= 15 and len(lp.recovery_patterns) >= 15
    for p in lp.success_patterns + lp.recovery_patterns:
        assert isinstance(p.compiled(), re.Pattern) and not p.private
    hit = lambda pats, s: any(p.compiled().search(s) for p in pats)
    assert hit(lp.success_patterns, "Great, your appointment is confirmed for Friday.")
    assert hit(lp.success_patterns, "You're all set.")
    assert not hit(lp.success_patterns, "Let me check availability for Friday.")
    assert hit(lp.recovery_patterns, "Sorry, I didn't catch that. Could you repeat it?")
    assert hit(lp.recovery_patterns, "Could you spell your last name?")
    assert not hit(lp.recovery_patterns, "Your appointment is on Friday at three.")


def test_normalizer_rules_mirror_code():
    rules = LangPack.load(PUB).normalizer_rules
    assert rules["normalizer_version"] == nz.NORMALIZER_VERSION
    assert rules["entity_normalizer_version"] == nz.ENTITY_NORMALIZER_VERSION
    assert rules["entity"]["nato"] == nz.NATO
    assert rules["entity"]["email_separators"] == nz._EMAIL_SEP
    assert set(rules["entity"]["zip_cues"]) == nz._ZIP_CUES


def test_public_hash_stable(tmp_path):
    a, b = LangPack.load(PUB), LangPack.load(PUB)
    assert a.hash == b.hash == a.public_hash and len(a.hash) == 64
    # comments and line endings do not change the hash; content does
    copy = tmp_path / "en-US"
    copy.mkdir()
    for f in PUB.iterdir():
        text = f.read_text(encoding="utf-8")
        (copy / f.name).write_bytes(("# extra comment\n" + text).replace("\n", "\r\n").encode())
    assert LangPack.load(copy).hash == a.hash
    y = copy / "hbc_tokens.yaml"
    y.write_text(y.read_text(encoding="utf-8").replace("  - sure", "  - sure\n  - yep"),
                 encoding="utf-8")
    assert LangPack.load(copy).hash != a.hash


def _private(tmp_path) -> Path:
    priv = tmp_path / "private-repo" / "langpacks" / "en-US"
    priv.mkdir(parents=True)
    (priv / "hbc_tokens.yaml").write_text("tokens: [yep]\n", encoding="utf-8")
    (priv / "success_patterns.yaml").write_text(
        "patterns:\n  - id: p01\n    regex: 'held-out phrasing'\n", encoding="utf-8")
    return priv


def test_private_merged_only_when_env_set(tmp_path, monkeypatch):
    priv = _private(tmp_path)
    base = LangPack.load(PUB)
    ignored = LangPack.load(PUB, priv)                 # env unset -> not merged
    assert ignored.private_ignored and ignored.private_dir is None
    assert ignored.hash == base.hash and "yep" not in ignored.hbc_tokens

    monkeypatch.setenv("DUPLEX_PRIVATE_DIR", str(tmp_path / "private-repo"))
    merged = LangPack.load(PUB)                        # default: $DIR/langpacks/<lang>
    assert merged.private_dir == priv
    assert merged.hbc_tokens == SPEC_HBC + ("yep",)
    assert merged.success_patterns[-1].id == "p01" and merged.success_patterns[-1].private
    assert merged.hash != base.hash and merged.public_hash == base.hash
    assert LangPack.load(PUB, priv).hash == merged.hash


def test_env_set_without_private_pack_keeps_public_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("DUPLEX_PRIVATE_DIR", str(tmp_path / "empty"))
    lp = LangPack.load(PUB)
    assert lp.private_dir is None and lp.hash == lp.public_hash


def test_private_inside_repo_refused(monkeypatch):
    monkeypatch.setenv("DUPLEX_PRIVATE_DIR", str(ROOT))
    with pytest.raises(LangPackError, match="inside the harness repo"):
        LangPack.load(PUB, ROOT / "configs")


def test_duplicate_private_pattern_id_refused(tmp_path, monkeypatch):
    priv = _private(tmp_path)
    (priv / "recovery_patterns.yaml").write_text(
        "patterns:\n  - id: r01\n    regex: 'x'\n", encoding="utf-8")
    monkeypatch.setenv("DUPLEX_PRIVATE_DIR", str(tmp_path / "private-repo"))
    with pytest.raises(LangPackError, match="duplicate pattern id"):
        LangPack.load(PUB)


def test_fixture_scenarios_validate_against_langpack():
    from duplexbench.scenarios.loader import load_pack, validate
    from duplexbench.thresholds import Thresholds
    fix = ROOT / "tests" / "fixtures" / "scenarios" / "public-dev"
    lp = LangPack.load(PUB)
    pack = load_pack(fix, hbc_tokens=lp.hbc_tokens, check_tools=False)
    ids = {s.id for s in pack.scenarios}
    assert {"track_a_002", "track_c_001"} <= ids
    for s in pack.scenarios:
        assert validate(s, th=Thresholds(), hbc_tokens=lp.hbc_tokens, check_tools=False) == []
    a = next(s for s in pack.scenarios if s.id == "track_a_002")
    assert {o.cls for o in a.overlays} >= {"Y-INT", "H-BC", "P-PAUSE", "E-END"}
    c = next(s for s in pack.scenarios if s.id == "track_c_001")
    assert c.track == "C" and c.tools and c.required_spoken
