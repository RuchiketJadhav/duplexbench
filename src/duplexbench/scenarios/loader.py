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
             repo_root: Path | None = None) -> list[str]:
    """Return every semantic error in `scenario` (empty list = valid). M2-T01 errors:

    overlay anchor not a step; Y-INT without >= 3 words or without
    `response_anchors`; H-BC token not in the langpack list; P-PAUSE `pause_ms`
    outside `th.pause_ms`; placement `in_pause` on a step without `pause_ms`;
    E-END without >= `th.eend_silence_ms` silence (`resumption_ms`); private
    split loaded from inside the repo (only when `path` is given).
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
        if o.cls == "E-END" and (o.resumption_ms is None or o.resumption_ms < th.eend_silence_ms):
            errs.append(f"{where}: E-END needs >= {th.eend_silence_ms} ms silence "
                        f"(resumption_ms={o.resumption_ms})")
    if path is not None and scenario.split == "private-test":
        root = repo_root or REPO_ROOT
        if _is_under(path, root):
            errs.append(f"{sid}: private-test scenario loaded from inside the repo ({path}); "
                        f"private scenarios live under ${PRIVATE_ENV}")
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


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def load_pack(dir: Path | str, *, th: Thresholds | None = None,
              hbc_tokens: Collection[str] | None = None,
              repo_root: Path | None = None) -> Pack:
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
        errs += validate(s, th=th, hbc_tokens=hbc_tokens, path=f, repo_root=repo_root)
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
    h.update(canonical_json([s.model_dump(mode="json") for s in ordered]).encode())
    for s in ordered:
        for name, p in sorted(clips[s.id]):
            h.update(f"\n{s.id}:{name}:{_sha256_file(p)}".encode())
    return Pack(scenarios=ordered, hash=h.hexdigest(), paths=paths)
