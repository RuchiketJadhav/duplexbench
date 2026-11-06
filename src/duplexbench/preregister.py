"""Pre-registration: hash everything a run's numbers depend on, before the run (BUILD_PLAN M2b-T02; spec §10 item 1, §11).

    collect_hashes()            -> {"systems": {name: config_id}, "pack", "langpack", "thresholds",
                                    "private_tree", "harness_commit"}
    write_record(hashes)        -> results/preregister-<UTC ts>.json
    check_against_latest(cur)   -> [] when every hash equals the latest record, else one line per diff
                                   (`cli run` refuses on a non-empty list)
    refusal_message(diffs)      -> one sentence naming the stale record and the hash groups that differ
                                   (systems/pack/langpack/thresholds/private/commit)

Record JSON:

    {"schema": 1, "created_utc": "2026-11-04T12:00:00.123456Z",
     "hashes": {"systems": {"<file stem>": "<config_id>", ...},
                "pack": "<sha256>" | "missing",
                "langpack": "<sha256>" | "missing",
                "thresholds": "<sha256>",
                "private_tree": "<sha256>" | "unmounted",
                "harness_commit": "<git sha>" | null}}

`harness_commit` is null when the tree is not a git checkout (printed "no commit"); records
written before fix-22 carry the legacy string "unknown", which compares equal to null.
`latest_record` orders records by their timestamp (`created_utc`, else the file name), never
by file name alone, so names written at different precisions still sort correctly.

Only the private tree's hash is stored, never its contents (spec §11, eng review D5).
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from duplexbench.storage import canonical_json, config_id
from duplexbench.thresholds import Thresholds

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEMS_DIR = REPO_ROOT / "configs" / "systems"
PACK_DIR = REPO_ROOT / "scenarios" / "public-dev"
LANGPACK_DIR = REPO_ROOT / "configs" / "langpacks" / "en-US"
RESULTS_DIR = REPO_ROOT / "results"
PRIVATE_ENV = "DUPLEX_PRIVATE_DIR"
RECORD_GLOB = "preregister-*.json"
SKIP_DIRS = {".git", "__pycache__"}
NO_COMMIT = "no commit"
LEGACY_NO_COMMIT = ("unknown", "None", "")     # pre-fix-22 spellings of "no commit"
# hash key -> group named in refusals
GROUPS = {"systems": "systems", "pack": "pack", "langpack": "langpack",
          "thresholds": "thresholds", "private_tree": "private", "harness_commit": "commit"}
_NAME_TS = re.compile(r"^preregister-(\d{8}T\d{6})(\d{0,6})Z$")


class PreregisterError(ValueError):
    """A pre-registration record cannot be read or ordered."""


def _sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def tree_hash(root: Path) -> str:
    """sha256 over sorted (relative posix path, file sha256) of every file under `root`."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*"), key=lambda q: q.relative_to(root).as_posix()):
        rel = p.relative_to(root)
        if p.is_file() and not (set(rel.parts) & SKIP_DIRS):
            h.update(f"{rel.as_posix()}\0{_sha_file(p)}\n".encode())
    return h.hexdigest()


def systems_hashes(systems_dir: Path = SYSTEMS_DIR) -> dict[str, str]:
    """config_id of every `configs/systems/*.yaml` (as loaded; resolution arrives with `cli run`)."""
    if not systems_dir.is_dir():
        return {}
    return {p.stem: config_id(yaml.safe_load(p.read_text(encoding="utf-8")) or {})
            for p in sorted(systems_dir.glob("*.yaml"))}


def pack_hash(pack_dir: Path = PACK_DIR, th: Thresholds | None = None) -> str:
    from duplexbench.scenarios.loader import load_pack
    if not pack_dir.is_dir() or not any(pack_dir.rglob("*.y*ml")):
        return "missing"
    return load_pack(pack_dir, th=th).hash


def langpack_hash(public_dir: Path = LANGPACK_DIR, private_dir: Path | None = None) -> str:
    """`stimuli.langpack.LangPack.load(...).hash` when that module exists, else the public dir tree hash."""
    try:
        from duplexbench.stimuli.langpack import LangPack  # M2-T02, may not exist yet
    except ImportError:
        LangPack = None
    if LangPack is not None:
        return LangPack.load(public_dir, private_dir).hash
    if not public_dir.is_dir():
        return "missing"
    return tree_hash(public_dir)


def private_dir() -> Path | None:
    v = os.environ.get(PRIVATE_ENV, "").strip()
    return Path(v) if v and Path(v).is_dir() else None


def harness_commit(repo_root: Path = REPO_ROOT) -> str | None:
    """HEAD sha, or None (recorded as JSON null) when there is no git commit."""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True,
                           text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def _commit(v: Any) -> str | None:
    """Normalize a recorded commit: None and the legacy spellings mean no commit."""
    return None if v is None or (isinstance(v, str) and v in LEGACY_NO_COMMIT) else v


def show_value(key: str, v: Any) -> str:
    """How a hash value is printed (`harness_commit` None -> "no commit")."""
    if key.split("/")[0] == "harness_commit" and _commit(v) is None:
        return NO_COMMIT
    return str(v)


def collect_hashes(*, systems_dir: Path = SYSTEMS_DIR, pack_dir: Path = PACK_DIR,
                   langpack_dir: Path = LANGPACK_DIR, thresholds_path: Path | None = None,
                   private: Path | None | str = "env", repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    """Every hash a pre-registration fixes. `private="env"` reads `$DUPLEX_PRIVATE_DIR`."""
    th = Thresholds.load(thresholds_path)
    priv = private_dir() if private == "env" else (Path(private) if private else None)
    lp_private = priv / "langpacks" / langpack_dir.name if priv else None
    return {
        "systems": systems_hashes(systems_dir),
        "pack": pack_hash(pack_dir, th),
        "langpack": langpack_hash(langpack_dir, lp_private if lp_private and lp_private.is_dir() else None),
        "thresholds": th.hash(),
        "private_tree": tree_hash(priv) if priv and priv.is_dir() else "unmounted",
        "harness_commit": harness_commit(repo_root),
    }


def write_record(hashes: dict[str, Any], results_dir: Path = RESULTS_DIR) -> Path:
    """Write `results/preregister-<ts>.json`; never overwrites an existing record."""
    results_dir.mkdir(parents=True, exist_ok=True)
    now = _dt.datetime.now(_dt.timezone.utc)
    path = results_dir / f"preregister-{now.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    while path.exists():           # coarse wall clock (Windows): step 1 us so names stay ordered
        now += _dt.timedelta(microseconds=1)
        path = results_dir / f"preregister-{now.strftime('%Y%m%dT%H%M%S%fZ')}.json"
    rec = {"schema": 1, "created_utc": now.isoformat().replace("+00:00", "Z"), "hashes": hashes}
    path.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _parse_utc(v: Any) -> _dt.datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        t = _dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


def _name_utc(path: Path) -> _dt.datetime | None:
    m = _NAME_TS.match(path.stem)
    if not m:
        return None
    base = _dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=_dt.timezone.utc)
    micro = int(m.group(2).ljust(6, "0")) if m.group(2) else 0
    return base + _dt.timedelta(microseconds=micro)


def record_time(path: Path, rec: dict[str, Any]) -> _dt.datetime:
    """A record's timestamp: `created_utc`, else the one in its file name."""
    t = _parse_utc(rec.get("created_utc")) or _name_utc(path)
    if t is None:
        raise PreregisterError(f"{path.name}: no readable created_utc and no timestamp in the name")
    return t


def latest_record(results_dir: Path = RESULTS_DIR) -> tuple[Path, dict[str, Any]] | None:
    """The newest record by timestamp (ties: file name), whatever the name's precision."""
    if not results_dir.is_dir():
        return None
    recs = []
    for p in results_dir.glob(RECORD_GLOB):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise PreregisterError(f"{p.name}: unreadable pre-registration record ({e})") from e
        recs.append((record_time(p, rec), p.name, p, rec))
    if not recs:
        return None
    _, _, path, rec = max(recs, key=lambda r: (r[0], r[1]))
    return path, rec


_ABSENT = object()


def diff_hashes(old: dict[str, Any], new: dict[str, Any], prefix: str = "") -> list[str]:
    """One `key: old -> new` line per differing leaf; nested dicts use `a/b` keys.

    A key only one side has reads `key: added <new>` / `key: removed (was <old>)`;
    `harness_commit` None and the legacy "unknown" are equal and print as "no commit"."""
    out: list[str] = []
    for k in sorted(set(old) | set(new)):
        key = f"{prefix}{k}"
        a, b = old.get(k, _ABSENT), new.get(k, _ABSENT)
        if isinstance(a, dict) and isinstance(b, dict):
            out += diff_hashes(a, b, f"{key}/")
            continue
        if key == "harness_commit":
            a = None if a is _ABSENT else _commit(a)
            b = None if b is _ABSENT else _commit(b)
        if a is _ABSENT:
            out.append(f"{key}: added {show_value(key, b)}")
        elif b is _ABSENT:
            out.append(f"{key}: removed (was {show_value(key, a)})")
        elif canonical_json(a) != canonical_json(b):
            out.append(f"{key}: {show_value(key, a)} -> {show_value(key, b)}")
    return out


def group_of(line: str) -> str:
    """The refusal group (systems/pack/langpack/thresholds/private/commit) of a diff line."""
    top = line.split(":", 1)[0].split("/", 1)[0]
    return GROUPS.get(top, top)


def check_against_latest(current_hashes: dict[str, Any], results_dir: Path = RESULTS_DIR) -> list[str]:
    """Diff lines between `current_hashes` and the latest record; [] means pre-registered.

    Each line reads `<record file>: <key>: <recorded> -> <current>`."""
    latest = latest_record(results_dir)
    if latest is None:
        return [f"no pre-registration record ({RECORD_GLOB}) in {results_dir}"]
    path, rec = latest
    return [f"{path.name}: {line}" for line in diff_hashes(rec.get("hashes", {}), current_hashes)]


def refusal_message(diffs: list[str]) -> str:
    """One sentence for a refusal: which record is stale and which hash groups differ.

    `diffs` is `check_against_latest`'s output; "" when it is empty."""
    if not diffs:
        return ""
    if diffs[0].startswith("no pre-registration record"):
        return diffs[0]
    name = diffs[0].split(": ", 1)[0]
    counts: dict[str, int] = {}
    for d in diffs:
        g = group_of(d.split(": ", 1)[1])
        counts[g] = counts.get(g, 0) + 1
    order = list(GROUPS.values())
    ranked = sorted(counts, key=lambda g: (order.index(g) if g in order else len(order), g))
    parts = [f"{g} ({counts[g]})" if g == "systems" else g for g in ranked]
    plural = "es" if len(diffs) != 1 else ""
    return (f"pre-registration {name} is stale: {', '.join(parts)} differ "
            f"({len(diffs)} hash{plural})")
