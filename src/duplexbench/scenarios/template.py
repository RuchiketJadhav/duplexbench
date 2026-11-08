"""Scenario template generator and per-file validator (BUILD_PLAN M2b-T01; spec §3.1, §3.2, §11).

    cli scenario new --domain D --track A --split public-dev   -> YAML, every field, a comment per field
    cli scenario validate <dir>                                -> table: file | id | status | problems

The template is valid YAML and schema-valid as generated; it carries `TODO`
placeholders that `todo_fields` reports, so an unedited template never passes
`validate_dir`. Numbers that come from thresholds (pause range, E-END silence,
overlay offsets) are filled from the `Thresholds` instance, never hard-coded.
Private-split templates are written under `$DUPLEX_PRIVATE_DIR` only (spec §11).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from duplexbench.scenarios.loader import _clip_paths, validate
from duplexbench.scenarios.schema import Scenario
from duplexbench.thresholds import Thresholds

TODO = "TODO"


def render_template(domain: str, track: str, split: str, *, scenario_id: str | None = None,
                    th: Thresholds | None = None) -> str:
    """A commented scenario YAML. Fields marked TODO must be filled before it validates."""
    th = th or Thresholds.load()
    if track not in ("A", "C", "D"):
        raise ValueError(f"track must be A, C or D, not {track!r}")
    if split not in ("public-dev", "private-test"):
        raise ValueError(f"split must be public-dev or private-test, not {split!r}")
    sid = scenario_id or f"{domain}_{track.lower()}_{TODO}"
    source = "simulated" if track == "C" else "scripted"
    p_lo, p_hi = th.pause_ms
    w_lo, w_hi = th.wait_resume_ms
    off = th.overlay_offsets_ms
    return f"""\
# Scenario template (spec §3.1). Replace every {TODO}; `duplexbench scenario validate <dir>` checks it.
# Overlay classes and correct behavior: spec §3.2. Private-test files never live in this repo (spec §11).
id: "{sid}"                  # unique scenario id; also the file name
version: "1"                 # bump on any content change (the pack hash changes anyway)
split: {split}               # public-dev | private-test
domain: "{domain}"           # domain backend name (spec §4.2)
track: {track}               # A floor control | C task | D entities
source: {source}             # scripted (A, D) | simulated (C) | recorded (v1.1)
speaker_id: null             # recorded sources only
caller_voice: "{TODO}"       # TTS voice id of the main caller (spec §3.5: >= 6 voices across the pack)
speaking_rate: 1.0           # TTS rate multiplier (two rates across the pack)
persona: null                # Track C simulator persona text, else null
goal: null                   # Track C simulator goal, else null
facts: {{}}                    # caller facts the simulator may reveal (Track C), e.g. {{name: "..."}}
steps:                       # ordered caller steps; agent turns are aligned by step id (spec §3.4)
  - id: s1                   # step id, referenced by overlays[].anchor_step
    text: "{TODO}"           # what the caller says
    clip: null               # audio path relative to this file (filled by `stimuli generate`)
    speech_onset_ms: null    # filled by the importer from the clip
    speech_end_ms: null      # filled by the importer from the clip
    pause_ms: null           # mid-turn pause for P-PAUSE / in_pause overlays, in [{p_lo}, {p_hi}] ms
    resumption_text: null    # scripted completion after the pause
overlays: []                 # overlay events; one example per kind below, uncomment and edit
#  - id: o1                  # overlay id, unique in the scenario
#    cls: Y-INT              # Y-INT | Y-CMD | H-BC | H-SIDE | H-BG | H-TIC | P-PAUSE | E-END
#    anchor_step: s1         # the caller step whose agent reply it lands in
#    placement: during_agent # during_agent | in_pause (needs the step's pause_ms)
#    offset_ms: {off[0]}           # after agent onset; pre-registered offsets {list(off)}
#    clip: null              # overlay audio (filled by `stimuli generate`)
#    text: "{TODO}"          # Y-INT: >= 3 words; H-BC: a langpack token; Y-CMD: a langpack command
#    level_db: 0.0           # H-SIDE {th.side_level_db} dB, H-BG {th.bg_level_db} dB (spec §3.2)
#    response_anchors: []    # Y-INT: words a relevant reply must contain (required)
#    resumption_ms: null     # wait-class Y-CMD: {w_lo}-{w_hi} ms; E-END: >= {th.eend_silence_ms} ms
#    voice_id: null          # a voice distinct from caller_voice when the class implies another speaker
tools: []                    # Track C mock tools
#  - name: "{TODO}"          # tool name the agent calls
#    kind: read              # read | write
#    latency_ms: 0           # injected latency; buckets {list(th.tool_latency_buckets_ms)} ms (spec §4.2)
#    failure: none           # none | timeout | error | partial
#    result_anchors: []      # words that carry the tool result in the agent's speech (D6)
expected_state: {{}}           # Track C end-state the backend must reach (spec §4.2)
required_spoken: []          # confirmations the agent must say, e.g. [{{text: "..."}}]
entities: []                 # Track D entities, e.g. [{{type: phone, value: "..."}}]
policy: {{}}                   # Track C policy rules (auth, forbidden actions)
seed: {{}}                     # Track C rows merged onto the backend SEED, {{table: {{row_id: {{field: v}}}}}}
conditions: [C0]             # channel conditions to run (spec §5)
trials: 1                    # trials per condition
control_twin: false          # true when the scenario has overlays (spec §3.4)
"""


def todo_fields(data: Any, prefix: str = "") -> list[str]:
    """Dotted paths of every string value that still contains TODO."""
    out: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            out += todo_fields(v, f"{prefix}{k}.")
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out += todo_fields(v, f"{prefix}{i}.")
    elif isinstance(data, str) and TODO in data:
        out.append(prefix.rstrip("."))
    return out


@dataclass
class FileResult:
    path: Path
    scenario_id: str | None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def validate_file(path: Path, *, th: Thresholds | None = None, hbc_tokens=None,
                  repo_root: Path | None = None) -> FileResult:
    """Schema + `loader.validate` + TODO placeholders + referenced clips for one YAML."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        return FileResult(path, None, [f"YAML: {e}"])
    sid = data.get("id") if isinstance(data, dict) else None
    probs = [f"{f}: still {TODO}" for f in todo_fields(data)]
    try:
        s = Scenario.model_validate(data)
    except ValidationError as e:
        return FileResult(path, sid, probs + [f"schema: {err['loc']}: {err['msg']}" for err in e.errors()])
    probs += validate(s, th=th, hbc_tokens=hbc_tokens, path=path, repo_root=repo_root)
    probs += [f"clip not found: {n}" for n, p in _clip_paths(s, path.parent) if not p.is_file()]
    if s.overlays and not s.control_twin:
        probs.append("has overlays but control_twin is false (spec §3.4)")
    return FileResult(path, s.id, probs)


def validate_dir(d: Path, **kw) -> list[FileResult]:
    files = sorted(list(Path(d).rglob("*.yaml")) + list(Path(d).rglob("*.yml")))
    return [validate_file(f, **kw) for f in files]


def format_table(results: list[FileResult], root: Path | None = None) -> str:
    rows = []
    for r in results:
        name = str(r.path.relative_to(root)) if root and r.path.is_relative_to(root) else str(r.path)
        first, *rest = r.problems or ["-"]
        rows.append([name, r.scenario_id or "?", "ok" if r.ok else "FAIL", first])
        rows += [["", "", "", p] for p in rest]
    head = ["file", "id", "status", "problems"]
    w = [max([len(head[i])] + [len(x[i]) for x in rows]) for i in range(3)]
    fmt = lambda c: "  ".join([c[0].ljust(w[0]), c[1].ljust(w[1]), c[2].ljust(w[2]), c[3]]).rstrip()  # noqa: E731
    n_bad = sum(not r.ok for r in results)
    return "\n".join([fmt(head)] + [fmt(x) for x in rows]
                     + [f"{len(results)} file(s), {n_bad} failing"]) + "\n"
