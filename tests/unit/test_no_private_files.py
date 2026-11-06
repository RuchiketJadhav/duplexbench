"""M0-T01 / spec §11 / eng review D5: nothing private is ever in the harness tree."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP = {".git", ".venv", "reference", "results", "__pycache__", ".pytest_cache"}


def _walk():
    stack = [ROOT]
    while stack:
        d = stack.pop()
        for p in d.iterdir():
            if p.is_dir():
                if p.name not in SKIP:
                    stack.append(p)
            else:
                yield p


def test_no_private_paths():
    bad = []
    for p in _walk():
        rel = p.relative_to(ROOT).as_posix()
        if "scenarios/private-test/" in rel or re.search(r"langpacks/[^/]+/private/", rel):
            bad.append(rel)
    assert bad == []


def test_no_private_test_scenario_yaml():
    bad = []
    for p in _walk():
        if p.suffix in (".yaml", ".yml"):
            text = p.read_text(encoding="utf-8", errors="replace")
            if re.search(r"^split:\s*['\"]?private-test", text, re.M):
                bad.append(p.relative_to(ROOT).as_posix())
    assert bad == []
