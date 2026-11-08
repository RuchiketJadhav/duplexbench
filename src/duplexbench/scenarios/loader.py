"""Scenario pack loader and validator (BUILD_PLAN §2.5, M2-T01; spec §3.1, §3.2, §11).

    pack = load_pack(Path("scenarios/public-dev"))
    pack.hash   # sha256 over canonical scenario JSON (sorted by id) + sha256 of every referenced clip

Private split (spec §11, eng review D5): `private-test` scenarios live in a
separate private repo mounted at `$DUPLEX_PRIVATE_DIR`; `load_pack` refuses a
private-test scenario whose file lies under the harness repo root.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import ValidationError

from duplexbench.scenarios.schema import Scenario
from duplexbench.storage import canonical_json
from duplexbench.thresholds import Thresholds

REPO_ROOT = Path(__file__).resolve().parents[3]
PRIVATE_ENV = "DUPLEX_PRIVATE_DIR"

# Public H-BC list, spec §3.2. The authoritative list is the language pack
# (configs/langpacks/en-US/hbc_tokens.yaml, M2-T02); pass `hbc_tokens=langpack...`
# once it exists. This default only mirrors the spec's public list.
SPEC_HBC_TOKENS: tuple[str, ...] = ("mm-hm", "uh-huh", "yeah", "right", "okay", "got it", "sure")

YINT_MIN_WORDS = 3   # spec §3.2 "≥ 3 words" is a class definition, not a [Proposed] threshold


class PackError(ValueError):
    """A pack cannot be loaded; `errors` lists every problem found."""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Pack:
    scenarios: list[Scenario]
    hash: str
    paths: dict[str, Path]        # scenario id -> YAML path


def private_root() -> Path | None:
    """`$DUPLEX_PRIVATE_DIR` as a resolved path, or None when unset/empty."""
    v = os.environ.get(PRIVATE_ENV, "").strip()
    return Path(v).resolve() if v else None


def _norm_token(s: str) -> str:
    return re.sub(r"[^\w\s'-]", "", s.lower()).strip()


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def validate(scenario: Scenario, *, th: Thresholds | None = None,
             hbc_tokens: Collection[str] | None = None, path: Path | None = None,
             repo_root: Path | None = None, check_tools: bool = True) -> list[str]:
    """Return every semantic error in `scenario` (empty list = valid). M2-T01 errors:

    overlay anchor not a step; Y-INT without >= 3 words or without
    `response_anchors`; H-BC token not in the langpack list; P-PAUSE `pause_ms`
    outside `th.pause_ms`; placement `in_pause` on a step without `pause_ms`;
    E-END without >= `th.eend_silence_ms` silence (`resumption_ms`); private
    split loaded from inside the repo (only when `path` is given); overlay
    `resumption_text` on anything but a Y-CMD with `resumption_ms`; (fix-33) a tool
    name the domain's mock backend lacks, or an expectation naming an unknown tool or
    tool parameter (`_tool_errors`). `check_tools=False` skips that check, for scoring
    fixtures whose tool names are not backend tools (tests/fixtures track_c_001).
    """
    th = th or Thresholds.load()
    tokens = {_norm_token(t) for t in (hbc_tokens if hbc_tokens is not None else SPEC_HBC_TOKENS)}
    sid = scenario.id
    errs: list[str] = []
    steps = {s.id: s for s in scenario.steps}
    if len(steps) != len(scenario.steps):
        errs.append(f"{sid}: duplicate step id")
    if len({o.id for o in scenario.overlays}) != len(scenario.overlays):
        errs.append(f"{sid}: duplicate overlay id")
    for t in scenario.tools:
        if t.anchor_step is not None and t.anchor_step not in steps:
            errs.append(f"{sid}/tool {t.name}: anchor_step {t.anchor_step!r} is not a step")
    lo, hi = th.pause_ms
    for o in scenario.overlays:
        where = f"{sid}/{o.id} ({o.cls})"
        step = steps.get(o.anchor_step)
        if step is None:
            errs.append(f"{where}: anchor_step {o.anchor_step!r} is not a step")
        if o.cls == "Y-INT":
            if len(o.text.split()) < YINT_MIN_WORDS:
                errs.append(f"{where}: Y-INT needs >= {YINT_MIN_WORDS} words")
            if not o.response_anchors:
                errs.append(f"{where}: Y-INT needs response_anchors")
        if o.cls == "H-BC" and _norm_token(o.text) not in tokens:
            errs.append(f"{where}: H-BC token {o.text!r} not in the langpack list")
        if o.cls == "P-PAUSE" and step is not None:
            if step.pause_ms is None or not (lo <= step.pause_ms <= hi):
                errs.append(f"{where}: P-PAUSE pause_ms {step.pause_ms} outside [{lo},{hi}]")
        if o.placement == "in_pause" and step is not None and step.pause_ms is None:
            errs.append(f"{where}: placement in_pause on step {step.id!r} without pause_ms")
        if o.resumption_text is not None and (o.cls != "Y-CMD" or o.resumption_ms is None):
            errs.append(f"{where}: resumption_text needs a Y-CMD with resumption_ms")
        if o.cls == "E-END" and (o.resumption_ms is None or o.resumption_ms < th.eend_silence_ms):
            errs.append(f"{where}: E-END needs >= {th.eend_silence_ms} ms silence "
                        f"(resumption_ms={o.resumption_ms})")
    errs.extend(_seed_errors(scenario))
    if check_tools:
        errs.extend(_tool_errors(scenario))
    if path is not None and scenario.split == "private-test":
        root = repo_root or REPO_ROOT
        if _is_under(path, root):
            errs.append(f"{sid}: private-test scenario loaded from inside the repo ({path}); "
                        f"private scenarios live under ${PRIVATE_ENV}")
    return errs


def _seed_errors(s: Scenario) -> list[str]:
    """A `seed` block names only tables the domain backend has, and the merged seed is
    consistent (`Backend.seed_errors`). Also run for billing policy checks without a seed."""
    from duplexbench.tools.mock_backends import BACKENDS
    cls = BACKENDS.get(s.domain)
    if cls is None:
        return [f"{s.id}: seed given for domain {s.domain!r} with no mock backend"] if s.seed else []
    errs = [f"{s.id}/seed: unknown table {t!r} for {s.domain} (have {sorted(cls.SEED)})"
            for t in s.seed if t not in cls.SEED]
    errs += [f"{s.id}/seed: table {t!r} must map row ids to rows"
             for t, rows in s.seed.items() if not isinstance(rows, dict)
             or not all(isinstance(r, dict) for r in rows.values())]
    if errs:
        return errs
    try:
        b = cls(policy=s.policy, seed_overlay=s.seed or None)
    except Exception as e:  # noqa: BLE001 - report, do not crash validate
        return [f"{s.id}/seed: {e}"]
    return [f"{s.id}/seed: {m}" for m in b.seed_errors()]


def _tool_errors(s: Scenario) -> list[str]:
    """fix-33: every `tools[].name` is a tool of the domain's mock backend (`Backend.tools()`),
    so a misspelt name fails here instead of at run start (`ToolServer.register_call`).
    Expectations that name a tool are checked too: `required_spoken[].after_tool` must be a
    scenario tool, and an expectation dict that names a `tool` with `arg` / `args` must use
    parameter names from that tool's schema."""
    if not s.tools and not any(rs.get("after_tool") for rs in s.required_spoken):
        return []
    from duplexbench.tools.mock_backends import BACKENDS
    cls = BACKENDS.get(s.domain)
    if cls is None:
        return [f"{s.id}/tool {t.name}: domain {s.domain!r} has no mock backend "
                f"(have {sorted(BACKENDS)})" for t in s.tools]
    try:
        schemas = {t.name: t for t in cls().tools()}
    except Exception as e:  # noqa: BLE001 - report, do not crash validate
        return [f"{s.id}/tools: cannot list {s.domain} backend tools: {e}"]
    errs = [f"{s.id}/tool {t.name}: not a tool of the {s.domain} mock backend "
            f"(have {sorted(schemas)})" for t in s.tools if t.name not in schemas]
    errs += [f"{s.id}/tool {t.name}: kind {t.kind!r} but the {s.domain} backend says "
             f"{schemas[t.name].kind!r}" for t in s.tools
             if t.name in schemas and t.kind != schemas[t.name].kind]
    names = {t.name for t in s.tools}
    for where, items in (("required_spoken", s.required_spoken), ("entities", s.entities)):
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            label = f"{s.id}/{where} {item.get('id', i)}"
            after = item.get("after_tool")
            if after and after not in names:
                errs.append(f"{label}: after_tool {after!r} is not in the scenario's tools "
                            f"(have {sorted(names)})")
            tool = item.get("tool")
            if tool is None:
                continue
            if tool not in schemas:
                errs.append(f"{label}: tool {tool!r} is not a tool of the {s.domain} mock "
                            f"backend (have {sorted(schemas)})")
                continue
            arg = item.get("arg", item.get("args"))
            args = [arg] if isinstance(arg, str) else list(arg or [])
            known = set(schemas[tool].parameters.get("properties", {}))
            errs += [f"{label}: tool {tool} has no parameter {a!r} (have {sorted(known)})"
                     for a in args if a not in known]
    return errs


def _clip_paths(s: Scenario, base: Path) -> list[tuple[str, Path]]:
    """Referenced clips (must exist) plus resumption clips that exist (convention below);
    a resumption file enters the pack hash only when present, so packs without one hash
    exactly as before the convention."""
    names = [st.clip for st in s.steps if st.clip] + [o.clip for o in s.overlays if o.clip]
    out = [(n, (base / n)) for n in names]
    req, opt = clip_sources(s, base)
    for key, p in {**req, **opt}.items():
        if key.endswith("/resumption") and p.is_file():
            out.append((p.relative_to(base).as_posix(), p))
    return out


RESUMPTION_SUFFIX = "_resumption"


def resumption_clip_path(clip: Path | str) -> Path:
    """Resumption clip convention (BUILD_PLAN §2.5 note): `<clip stem>_resumption<ext>` next
    to the clip. Used for a step with `pause_ms` (key `<step>/resumption`) and for a
    wait-class Y-CMD overlay with `resumption_ms` (key `<overlay>/resumption`)."""
    c = Path(clip)
    return c.with_name(f"{c.stem}{RESUMPTION_SUFFIX}{c.suffix}")


def clip_sources(s: Scenario, base: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """(required, optional) clip-key -> path maps for a scripted scenario, keyed as the
    ScriptedCaller / OverlayEngine `clips` mapping expects (`s1`, `s1/resumption`,
    `o4`, `o4/resumption`). Step resumptions are required; an overlay resumption is
    optional here (only a wait-class Y-CMD needs it; the engine raises if it is missing).
    Not part of `load_pack` validation, so packs without resumption files still load."""
    req: dict[str, Path] = {}
    opt: dict[str, Path] = {}
    for st in s.steps:
        if not st.clip:
            continue
        req[st.id] = base / st.clip
        if st.pause_ms is not None:
            req[f"{st.id}/resumption"] = resumption_clip_path(base / st.clip)
    for o in s.overlays:
        if not o.clip:
            continue
        req.setdefault(o.id, base / o.clip)
        if o.resumption_ms is not None:
            opt[f"{o.id}/resumption"] = resumption_clip_path(base / o.clip)
    return req, opt


def _hash_dump(s: Scenario) -> dict:
    """Scenario JSON for the pack hash. An unset overlay `resumption_text` (field added
    by fix-31) is left out, so packs that do not use it hash exactly as before."""
    d = s.model_dump(mode="json")
    if not d.get("seed"):
        d.pop("seed", None)         # field added for G2/G4; empty seed hashes as before
    for o in d.get("overlays", []):
        if o.get("resumption_text") is None:
            o.pop("resumption_text", None)
    return d


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_pack(dir: Path | str, *, th: Thresholds | None = None,
              hbc_tokens: Collection[str] | None = None,
              repo_root: Path | None = None, check_tools: bool = True) -> Pack:
    """Load and validate every `*.yaml` / `*.yml` under `dir`; raise `PackError` on any error.

    Clip paths are relative to the scenario file's directory. The hash covers
    the canonical JSON of every scenario sorted by id plus the sha256 of every
    referenced clip (spec §3.1 "the pack has a content hash").
    """
    root = Path(dir)
    if not root.is_dir():
        raise PackError([f"pack directory not found: {root}"])
    files = sorted(list(root.rglob("*.yaml")) + list(root.rglob("*.yml")))
    errs: list[str] = []
    scenarios: dict[str, Scenario] = {}
    paths: dict[str, Path] = {}
    clips: dict[str, list[tuple[str, Path]]] = {}
    for f in files:
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            s = Scenario.model_validate(data)
        except (ValidationError, yaml.YAMLError) as e:
            errs.append(f"{f}: {e}")
            continue
        if s.id in scenarios:
            errs.append(f"{f}: duplicate scenario id {s.id!r} (also {paths[s.id]})")
            continue
        errs += validate(s, th=th, hbc_tokens=hbc_tokens, path=f, repo_root=repo_root,
                         check_tools=check_tools)
        scenarios[s.id] = s
        paths[s.id] = f
        clips[s.id] = _clip_paths(s, f.parent)
        for name, p in clips[s.id]:
            if not p.is_file():
                errs.append(f"{s.id}: referenced clip not found: {name}")
    if errs:
        raise PackError(errs)
    h = hashlib.sha256()
    ordered = [scenarios[k] for k in sorted(scenarios)]
    h.update(canonical_json([_hash_dump(s) for s in ordered]).encode())
    for s in ordered:
        for name, p in sorted(clips[s.id]):
            h.update(f"\n{s.id}:{name}:{_sha256_file(p)}".encode())
    return Pack(scenarios=ordered, hash=h.hexdigest(), paths=paths)
