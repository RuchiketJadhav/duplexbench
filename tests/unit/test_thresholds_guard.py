r"""BUILD_PLAN §2.6 literal guard: [Proposed] numbers live only in thresholds.py.

Walks src/duplexbench/**/*.py except thresholds.py; comments and string
literals (incl. docstrings) are blanked with `tokenize`, then each listed
number is searched as a standalone literal with `(?<![\w.])<num>(?![\w.])`.
A line is whitelisted only by a trailing `# threshold-ok <reason>` (or `# threshold-ok: <reason>`) comment
with a non-empty reason.
"""
import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "duplexbench"
NUMS = ["-45", "500", "60", "700", "300", "800", "1500", "2000", "5000", "6000", "480000",
        "20000", "0.7", "0.02", "1000"]
PATTERNS = [(n, re.compile(rf"(?<![\w.]){re.escape(n)}(?![\w.])")) for n in NUMS]
OK = re.compile(r"#\s*threshold-ok:?\s+\S")


def _code_lines(text: str) -> dict[int, str]:
    """line number -> code with comments and strings blanked out."""
    lines = text.splitlines()
    blank: dict[int, list[str]] = {i + 1: list(l) for i, l in enumerate(lines)}
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING) or (
                hasattr(tokenize, "FSTRING_MIDDLE") and tok.type == tokenize.FSTRING_MIDDLE):
            (r0, c0), (r1, c1) = tok.start, tok.end
            for r in range(r0, r1 + 1):
                row = blank.get(r)
                if row is None:
                    continue
                a = c0 if r == r0 else 0
                b = c1 if r == r1 else len(row)
                for c in range(a, min(b, len(row))):
                    row[c] = " "
    return {k: "".join(v) for k, v in blank.items()}


def find_violations(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    raw = text.splitlines()
    out = []
    for ln, code in _code_lines(text).items():
        if OK.search(raw[ln - 1]):
            continue
        for n, pat in PATTERNS:
            if pat.search(code):
                out.append(f"{path.relative_to(SRC.parent)}:{ln}: {n}: {raw[ln - 1].strip()}")
    return out


def test_no_threshold_literals_outside_thresholds_py():
    bad = []
    for p in sorted(SRC.rglob("*.py")):
        if p.name == "thresholds.py" and p.parent == SRC:
            continue
        bad += find_violations(p)
    assert bad == [], "\n".join(bad)


def test_guard_catches_and_whitelists(tmp_path):
    p = tmp_path / "x.py"
    p.write_text(
        "a = 500\n"
        "b = x - 45\n"           # binary minus with space: not the literal -45
        "c = -45\n"
        "d = 1500.0\n"           # different literal; not matched
        "e = 2000  # threshold-ok frame count of a fixture\n"
        "f = 2000  # threshold-ok\n"   # no reason -> still flagged
        "g = '500 ms'  # 500 in a comment\n"
        "h = 0.7\n"
        "i = 10_000\n",
        encoding="utf-8")
    SRC_ = SRC.parent
    hits = [h.split(": ")[0] for h in _violations_rel(p, SRC_)]
    assert hits == ["500", "-45", "2000", "0.7"]


def _violations_rel(p: Path, _root: Path) -> list[str]:
    text = p.read_text(encoding="utf-8")
    raw = text.splitlines()
    out = []
    for ln, code in _code_lines(text).items():
        if OK.search(raw[ln - 1]):
            continue
        for n, pat in PATTERNS:
            if pat.search(code):
                out.append(f"{n}: line {ln}")
    return out
