"""Pre-registration: hash everything a run's numbers depend on, before the run (BUILD_PLAN M2b-T02; spec §10 item 1, §11).

    collect_hashes()            -> {"systems": {name: config_id}, "pack", "langpack", "thresholds",
                                    "private_tree", "harness_commit", "run_config"}
    collect_for_run_config(p)   -> collect_hashes for a run config file, resolved as `cli run` does
    write_record(hashes)        -> results/preregister-<UTC ts>.json
    check_against_latest(cur)   -> [] when every hash equals the latest record, else one line per diff
                                   (`cli run` refuses on a non-empty list)
    refusal_message(diffs)      -> one sentence naming the stale record and the hash groups that differ
                                   (systems/pack/langpack/thresholds/private/commit/run-config)
                                   and every changed run-config field path

Record JSON:

    {"schema": 1, "created_utc": "2026-11-04T12:00:00.123456Z",
     "hashes": {"systems": {"<file stem>": "<config_id>", ...},
                "pack": "<sha256>" | "missing",
                "langpack": "<sha256>" | "missing",
                "thresholds": "<sha256>",
                "private_tree": "<sha256>" | "unmounted",
                "harness_commit": "<git sha>" | null,
                "run_config": {"sha256": "<sha256 of canonical fields>",
                               "fields": {"simulator.llm.model": "...", ...}}}}

`run_config` (fix-30) fixes the run config's `simulator` and `scripted` blocks, read
generically (whatever keys they hold), flattened to dotted leaf paths so a refusal names
the field that moved (`simulator.refasr.kwargs.pinned_model_uuid changed`). Lists are
leaves. Absent and empty blocks give no fields, so "absent on both sides" compares equal.
Never recorded: a secret-named key (api_key, key, token, secret, password, auth,
authorization, credential(s), bearer, cookie; `*_env` keys are variable NAMES and are kept),
URL userinfo and secret-named URL query parameters; nothing is read from the environment
(`base_url_env` stays a variable name). A `prompt` / `*_prompt` string becomes
`<key>_sha256`: the sha256 of the prompt file under simulator/prompts (CRLF-normalized,
as `caller_llm.prompt_hash`) when it names one, else of the text itself; a `simulator.llm`
block without `prompt` records the default prompt's hash. A record without `run_config`
(written before fix-30) is refused as "predates run-config hashing: re-register".

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
          "thresholds": "thresholds", "private_tree": "private", "harness_commit": "commit",
          "run_config": "run-config"}
RUN_CONFIG_KEY = "run_config"
RUN_CONFIG_BLOCKS = ("simulator", "scripted")
PREDATES = "predates run-config hashing: re-register"
_SECRET_KEY = re.compile(r"(^|_)(api_?key|key|token|secret|password|passwd|auth|authorization|"
                         r"credentials?|bearer|cookie)$", re.IGNORECASE)
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_PROMPT_KEY = re.compile(r"(^|_)prompt$")
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


def _is_secret_key(k: Any) -> bool:
    k = str(k)
    return not k.lower().endswith("_env") and bool(_SECRET_KEY.search(k))


def _scrub_url(s: str) -> str:
    """Drop userinfo and secret-named query parameters from a URL; other strings pass."""
    if not _URL.match(s):
        return s
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    u = urlsplit(s)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not _is_secret_key(k)]
    return urlunsplit((u.scheme, u.netloc.rsplit("@", 1)[-1], u.path, urlencode(q), u.fragment))


def _prompt_sha(v: str) -> str:
    """sha256 of the named prompt file (simulator/prompts), else of the text itself."""
    from duplexbench.simulator.caller_llm import PROMPTS_DIR, prompt_hash
    try:
        f = PROMPTS_DIR / v
        if len(v) < 256 and "\n" not in v and f.is_file():
            return prompt_hash(f)
    except (OSError, ValueError):
        pass
    return hashlib.sha256(v.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _plain(v: Any) -> Any:
    """A JSON-able copy: pydantic models dumped, mappings as dicts, sequences as lists."""
    if hasattr(v, "model_dump"):
        v = v.model_dump()
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return json.loads(canonical_json(v))


def _sanitize(v: Any) -> Any:
    """Secrets out, prompts hashed, URLs scrubbed (see the module docstring)."""
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        for k, x in v.items():
            if _is_secret_key(k):
                continue
            if _PROMPT_KEY.search(k) and isinstance(x, str):
                out[f"{k}_sha256"] = _prompt_sha(x)
            else:
                out[k] = _sanitize(x)
        return out
    if isinstance(v, list):
        return [_sanitize(x) for x in v]
    return _scrub_url(v) if isinstance(v, str) else v


def _flatten(v: Any, prefix: str, out: dict[str, Any]) -> None:
    """Dotted leaf paths; a non-empty dict recurses, everything else is a leaf."""
    if isinstance(v, dict) and v:
        for k in sorted(v):
            _flatten(v[k], f"{prefix}.{k}", out)
    elif not (isinstance(v, dict) and "." in prefix):    # nested {} adds nothing
        out[prefix] = v


def run_config_fields(run_config: Any) -> dict[str, Any]:
    """Dotted leaf path -> value for the `simulator` and `scripted` blocks (fix-30).

    `run_config` is a RunConfig (or any object with those attributes), a mapping, or None.
    An absent, null or empty block gives no fields."""
    fields: dict[str, Any] = {}
    if run_config is None:
        return fields
    for name in RUN_CONFIG_BLOCKS:
        raw = (run_config.get(name) if isinstance(run_config, dict)
               else getattr(run_config, name, None))
        block = _plain(raw)
        if block is None or block == {}:
            continue
        if name == "simulator" and isinstance(block, dict) and isinstance(block.get("llm"), dict) \
                and "prompt" not in block["llm"]:
            from duplexbench.simulator.caller_llm import prompt_hash
            block["llm"] = {**block["llm"], "prompt_sha256": prompt_hash()}  # the default prompt
        _flatten(_sanitize(block), name, fields)
    return fields


def run_config_hashes(run_config: Any) -> dict[str, Any]:
    """{"sha256": <sha256 of the canonical fields>, "fields": run_config_fields(...)}."""
    fields = run_config_fields(run_config)
    return {"sha256": hashlib.sha256(canonical_json(fields).encode()).hexdigest(),
            "fields": fields}


def collect_hashes(*, systems_dir: Path = SYSTEMS_DIR, pack_dir: Path = PACK_DIR,
                   langpack_dir: Path = LANGPACK_DIR, thresholds_path: Path | None = None,
                   private: Path | None | str = "env", repo_root: Path = REPO_ROOT,
                   run_config: Any = None) -> dict[str, Any]:
    """Every hash a pre-registration fixes. `private="env"` reads `$DUPLEX_PRIVATE_DIR`.

    `run_config` (RunConfig, mapping or None) supplies the `simulator` / `scripted` blocks;
    None hashes them as absent."""
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
        RUN_CONFIG_KEY: run_config_hashes(run_config),
    }


def collect_for_run_config(path: Path | str, *, pack_dir: Path | None = None,
                           private: Path | None | str = "env") -> tuple[dict[str, Any], Path]:
    """(hashes, results_dir) for the run config at `path`, resolving pack / systems_dir /
    thresholds / results_dir exactly as `cli_run.plan_run` does; `pack_dir` overrides."""
    from duplexbench.cli_run import RunConfig, _path
    cfg = RunConfig.load(path)
    th_path = _path(cfg.thresholds, None) if cfg.thresholds else None
    hashes = collect_hashes(
        systems_dir=_path(cfg.systems_dir, SYSTEMS_DIR),
        pack_dir=pack_dir or _path(cfg.pack, PACK_DIR),
        thresholds_path=th_path, private=private, run_config=cfg)
    return hashes, _path(cfg.results_dir, RESULTS_DIR)


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
        if key == RUN_CONFIG_KEY:
            out += diff_run_config(a, b)
            continue
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


def _show_field(v: Any) -> str:
    return v if isinstance(v, str) else canonical_json(v)


def diff_run_config(old: Any, new: Any) -> list[str]:
    """`run_config: <path> changed: <old> -> <new>` / `added: <new>` / `removed: <old>` per field.

    A record without the field (or with an unreadable one) gives the single line
    `run_config: predates run-config hashing: re-register`."""
    if old is _ABSENT or not isinstance(old, dict) or not isinstance(old.get("fields"), dict):
        return [f"{RUN_CONFIG_KEY}: {PREDATES}"]
    fa = old["fields"]
    fb = new.get("fields", {}) if isinstance(new, dict) else {}
    out: list[str] = []
    for p in sorted(set(fa) | set(fb)):
        if p not in fa:
            out.append(f"{RUN_CONFIG_KEY}: {p} added: {_show_field(fb[p])}")
        elif p not in fb:
            out.append(f"{RUN_CONFIG_KEY}: {p} removed: {_show_field(fa[p])}")
        elif canonical_json(fa[p]) != canonical_json(fb[p]):
            out.append(f"{RUN_CONFIG_KEY}: {p} changed: {_show_field(fa[p])} -> "
                       f"{_show_field(fb[p])}")
    if not out and isinstance(new, dict) and old.get("sha256") != new.get("sha256"):
        out.append(f"{RUN_CONFIG_KEY}: sha256 changed: {old.get('sha256')} -> {new.get('sha256')}")
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
    bodies = [d.split(": ", 1)[1] for d in diffs]
    rc = GROUPS[RUN_CONFIG_KEY]
    predates = f"{RUN_CONFIG_KEY}: {PREDATES}" in bodies
    if predates:
        bodies = [b for b in bodies if group_of(b) != rc]
    rc_changes = [b.split(": ", 1)[1].split(": ", 1)[0] for b in bodies if group_of(b) == rc]
    counts: dict[str, int] = {}
    for b in bodies:
        g = group_of(b)
        counts[g] = counts.get(g, 0) + 1
    order = list(GROUPS.values())
    ranked = sorted(counts, key=lambda g: (order.index(g) if g in order else len(order), g))
    parts = [f"{g} ({counts[g]})" if g in ("systems", rc) else g for g in ranked]
    plural = "es" if len(bodies) != 1 else ""
    stale = f"{', '.join(parts)} differ ({len(bodies)} hash{plural})"
    if predates:
        return f"pre-registration {name} {PREDATES}" + (f"; also stale: {stale}" if bodies else "")
    return (f"pre-registration {name} is stale: {stale}"
            + (f": {', '.join(rc_changes)}" if rc_changes else ""))
