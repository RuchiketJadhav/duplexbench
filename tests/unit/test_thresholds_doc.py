"""docs/thresholds.md is generated from Thresholds.items() and must be current (BUILD_PLAN M10)."""
import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gen_thresholds_doc.py"


def test_thresholds_doc_is_current():
    spec = importlib.util.spec_from_file_location("gen_thresholds_doc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DOC.read_text(encoding="utf-8") == mod.render(), \
        "run: uv run python scripts/gen_thresholds_doc.py"
