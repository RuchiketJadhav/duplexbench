"""M0-T01: the repo skeleton from CLAUDE.md §2 (as amended by BUILD_PLAN §1 row 3) exists.

Module files (runner.py, playout.py, ...) arrive with their own tasks; this test
checks the directories, packages and skeleton files M0-T01 creates.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

DIRS = [
    "configs/systems", "configs/conditions", "configs/runs", "configs/langpacks/en-US",
    "scenarios/public-dev", "docs", "tests/unit", "tests/integration", "tests/golden",
]
PACKAGES = [
    "", "channel", "stimuli", "scenarios", "adapters", "tools", "tools/mock_backends",
    "simulator", "simulator/prompts", "refasr", "metrics", "classifiers", "stats", "load",
    "transport", "report",
]
FILES = [
    "pyproject.toml", ".gitignore", ".env.example", "docs/worklog.md",
    "src/duplexbench/README.md",
]


@pytest.mark.parametrize("d", DIRS)
def test_dir_exists(d):
    assert (ROOT / d).is_dir(), d


@pytest.mark.parametrize("pkg", PACKAGES)
def test_package_exists(pkg):
    assert (ROOT / "src/duplexbench" / pkg / "__init__.py").is_file(), pkg


@pytest.mark.parametrize("f", FILES)
def test_file_exists(f):
    assert (ROOT / f).is_file(), f


def test_gitignore_excludes_private_and_results():
    text = (ROOT / ".gitignore").read_text()
    for pat in ("results/", "reference/", ".env"):
        assert pat in text, pat


def test_env_example_has_private_dir():
    assert "DUPLEX_PRIVATE_DIR=" in (ROOT / ".env.example").read_text()


def test_readme_has_four_task_diagram():
    text = (ROOT / "src/duplexbench/README.md").read_text(encoding="utf-8")
    for task in ("_send_caller", "_recv_agent", "_play", "_overlays"):
        assert task in text, task
