"""Write synthesized clips back into scripted scenario YAML (TODO fix-31; BUILD_PLAN M2-T04).

    plan = plan_pack(pack, client_id="fake", out_dir=Path("scenarios/public-dev/audio/tts"))
    conflicts(plan)                  # [] or one line per resumption-path clash
    changed = apply_plan(plan)       # after synthesis: resumption copies + YAML edits

For every step and overlay of a `source: scripted` scenario with non-empty text,
the clip becomes `<out_dir>/<sha>.wav` relative to the scenario file (the form
`load_pack` / `clip_sources` resolve), and a step gets `speech_onset_ms` /
`speech_end_ms` from the importer's detector (`importer.speech_bounds`, trim level
`ImportParams.trim_dbfs`) run on the synthesized audio. The clip itself is not
trimmed: the cached `.json` word timestamps stay on its timeline. The Overlay
schema has no onset/end fields, so overlays get only `clip`.

Resumption clips follow `loader.resumption_clip_path`: a step with `pause_ms` and
`resumption_text`, and a Y-CMD overlay with `resumption_ms` and `resumption_text`,
get a copy of the hash-named resumption clip (and its `.json`) at
`<clip stem>_resumption.wav`. Two items sharing one clip but needing different
resumption lines is a conflict (`conflicts`), reported before any synthesis.

A clip that is already set and lies outside `out_dir` (an imported recording) is
kept, with its onset/end. Simulated and recorded scenarios are not touched.

The YAML is edited line by line (PyYAML drops comments on a round trip), so
comments, key order and line endings survive. Each edit is checked by re-parsing;
if the line edit does not parse back to the expected data, the file is re-dumped
with PyYAML instead (comments other than the leading header block are then lost,
and `apply_plan` reports it). Idempotent: unchanged values and identical files are
not rewritten.
"""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import soundfile as sf
import yaml

from duplexbench.scenarios.loader import resumption_clip_path
from duplexbench.stimuli.importer import ImportParams, _mono_float, speech_bounds
from duplexbench.stimuli.tts import cache_key

_MS_PER_S = 1000              # threshold-ok: unit conversion ms per s
_STEP_FIELDS = ("clip", "speech_onset_ms", "speech_end_ms")


@dataclass
class ScenarioPlan:
    scenario_id: str
    yaml_path: Path
    steps: dict[str, dict] = field(default_factory=dict)       # step id -> {"wav": Path}
    overlays: dict[str, dict] = field(default_factory=dict)    # overlay id -> {"wav": Path}
    resumptions: dict[Path, Path] = field(default_factory=dict)  # convention path -> source wav
    kept: list[str] = field(default_factory=list)              # items with a non-TTS clip
    warnings: list[str] = field(default_factory=list)


@dataclass
class PackPlan:
    scenarios: list[ScenarioPlan]
    out_dir: Path
    clashes: list[str] = field(default_factory=list)


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _generated(clip: str | None, base: Path, out_dir: Path) -> bool:
    """True when `clip` is unset or points into the TTS cache dir (ours to rewrite)."""
    return not clip or _is_under(base / clip, out_dir)


def plan_pack(pack, *, client_id: str, out_dir: Path | str) -> PackPlan:
    """What the write-back will do for `pack` (a loader.Pack); reads nothing from disk."""
    out_dir = Path(out_dir)
    plans: list[ScenarioPlan] = []
    owner: dict[Path, tuple[Path, str]] = {}      # resumption path -> (source wav, who)
    clashes: list[str] = []

    def wav_for(voice: str, rate: float, text: str) -> Path:
        return out_dir / f"{cache_key(client_id, voice, rate, text)}.wav"

    def want_resumption(sp: ScenarioPlan, clip_wav: Path, src: Path, who: str) -> None:
        dst = resumption_clip_path(clip_wav)
        prev = owner.get(dst)
        if prev is not None and prev[0] != src:
            clashes.append(f"{who} and {prev[1]} share clip {clip_wav.name} but need different "
                           f"resumption lines ({dst.name}); give one of them a different text "
                           "or voice")
            return
        owner[dst] = (src, who)
        sp.resumptions[dst] = src

    for s in pack.scenarios:
        if s.source != "scripted":
            continue
        path = Path(pack.paths[s.id])
        base = path.parent
        sp = ScenarioPlan(scenario_id=s.id, yaml_path=path)
        rate = float(s.speaking_rate)
        for st in s.steps:
            if not st.text.strip():
                sp.warnings.append(f"{s.id}/{st.id}: empty text, no clip")
                continue
            if not _generated(st.clip, base, out_dir):
                sp.kept.append(f"{s.id}/{st.id}")
                continue
            w = wav_for(s.caller_voice, rate, st.text)
            sp.steps[st.id] = {"wav": w}
            if st.pause_ms is not None:
                if st.resumption_text and st.resumption_text.strip():
                    want_resumption(sp, w, wav_for(s.caller_voice, rate, st.resumption_text),
                                    f"{s.id}/{st.id}")
                else:
                    sp.warnings.append(f"{s.id}/{st.id}: pause_ms without resumption_text; "
                                       "no resumption clip written (cli run will refuse)")
        for o in s.overlays:
            if not o.text.strip():
                continue                                    # P-PAUSE, E-END: no audio
            if not _generated(o.clip, base, out_dir):
                sp.kept.append(f"{s.id}/{o.id}")
                continue
            voice = o.voice_id or s.caller_voice
            w = wav_for(voice, rate, o.text)
            sp.overlays[o.id] = {"wav": w}
            if o.resumption_text and o.resumption_text.strip():
                want_resumption(sp, w, wav_for(voice, rate, o.resumption_text),
                                f"{s.id}/{o.id}")
            elif o.cls == "Y-CMD" and o.resumption_ms is not None:
                sp.warnings.append(f"{s.id}/{o.id}: Y-CMD with resumption_ms but no "
                                   "resumption_text; no resumption clip written")
        plans.append(sp)
    return PackPlan(scenarios=plans, out_dir=out_dir, clashes=clashes)


def conflicts(plan: PackPlan) -> list[str]:
    return list(plan.clashes)


def onset_end_ms(wav: Path, params: ImportParams = ImportParams()) -> tuple[int, int]:
    """(speech onset, speech end) in ms within `wav`, by the importer's detector."""
    data, rate = sf.read(str(wav), always_2d=False)
    b = speech_bounds(_mono_float(data), int(rate), params)
    if b is None:
        raise ValueError(f"{wav}: no audio above {params.trim_dbfs} dBFS")
    ms = lambda n: int(round(n * _MS_PER_S / rate))
    return ms(b[0]), ms(b[1])


def _copy_if_changed(src: Path, dst: Path) -> bool:
    if dst.is_file() and dst.read_bytes() == src.read_bytes():
        return False
    shutil.copyfile(src, dst)
    return True


# -- line-based YAML editing ------------------------------------------------------------
_ID_RE = re.compile(r"^(\s*)-(\s+)id:\s*(.*?)\s*$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_code(line: str) -> bool:
    t = line.strip()
    return bool(t) and not t.startswith("#")


def _strip_comment(v: str) -> str:
    m = re.match(r"^(.*?)(\s+#.*)?$", v)
    return m.group(1) if m else v


def _scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    if re.fullmatch(r"[A-Za-z0-9_./-]+", s) and yaml.safe_load(s) == s:
        return s
    return json.dumps(s)


def _section(lines: list[str], name: str) -> tuple[int, int] | None:
    start = next((i for i, l in enumerate(lines)
                  if re.match(rf"^{re.escape(name)}:\s*(#.*)?$", l)), None)
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _is_code(lines[j]) and _indent(lines[j]) == 0:
            end = j
            break
    return start + 1, end


def _items(lines: list[str], lo: int, hi: int) -> dict[str, tuple[int, int, int]]:
    """item id -> (first line, end line exclusive (last code line + 1), key indent)."""
    starts = []
    for i in range(lo, hi):
        m = _ID_RE.match(lines[i])
        if m:
            starts.append((i, len(m.group(1)), len(m.group(1)) + 1 + len(m.group(2)),
                           yaml.safe_load(_strip_comment(m.group(3)) or "null")))
    out: dict[str, tuple[int, int, int]] = {}
    for k, (i, dash, key_ind, iid) in enumerate(starts):
        last = i
        for j in range(i + 1, hi):
            if _is_code(lines[j]):
                if _indent(lines[j]) <= dash:
                    break
                last = j
        out[str(iid)] = (i, last + 1, key_ind)
    return out


def _key_line(lines: list[str], a: int, b: int, ind: int, key: str) -> int | None:
    pat = re.compile(rf"^ {{{ind}}}{re.escape(key)}:(\s|$)")
    first = re.compile(rf"^\s*-\s+{re.escape(key)}:(\s|$)")
    for i in range(a, b):
        if pat.match(lines[i]) or (i == a and first.match(lines[i])):
            return i
    return None


def _key_end(lines: list[str], i: int, b: int, ind: int) -> int:
    """Line after the value of the key at line `i` (covers block scalars / nested lists)."""
    last = i
    for j in range(i + 1, b):
        if _is_code(lines[j]):
            if _indent(lines[j]) <= ind:
                break
            last = j
    return last + 1


def _set_fields(lines: list[str], section: str, item: str, fields: dict,
                order: tuple[str, ...]) -> list[str]:
    sec = _section(lines, section)
    if sec is None:
        raise KeyError(section)
    items = _items(lines, *sec)
    if item not in items:
        raise KeyError(f"{section}/{item}")
    for key in order:
        if key not in fields:
            continue
        a, b, ind = _items(lines, *_section(lines, section))[item]
        val = _scalar(fields[key])
        i = _key_line(lines, a, b, ind, key)
        if i is not None:
            m = re.match(r"^(.*?" + re.escape(key) + r":)(\s*)(.*)$", lines[i])
            head, rest = m.group(1), m.group(3)
            cm = re.search(r"(\s+)(#.*)$", rest)
            new = f"{head} {val}"
            if cm:                     # keep the comment at its column, or 2 spaces after a long value
                col = len(lines[i]) - len(cm.group(2))
                new = new.ljust(col) if len(new) + 2 <= col else new + "  "
                new += cm.group(2)
            lines[i] = new
            continue
        # insert after the previous field in `order` that exists, else after `text`, else at end
        after = None
        for prev in reversed(order[:order.index(key)]):
            after = _key_line(lines, a, b, ind, prev)
            if after is not None:
                break
        if after is None:
            after = _key_line(lines, a, b, ind, "text")
        at = _key_end(lines, after, b, ind) if after is not None else b
        lines.insert(at, f"{' ' * ind}{key}: {val}")
    return lines


def _expected(data: dict, upd_steps: dict, upd_ovs: dict) -> dict:
    d = copy.deepcopy(data)
    for st in d.get("steps") or []:
        st.update(upd_steps.get(str(st.get("id")), {}))
    for o in d.get("overlays") or []:
        o.update(upd_ovs.get(str(o.get("id")), {}))
    return d


def rewrite_yaml(path: Path, upd_steps: dict[str, dict], upd_ovs: dict[str, dict]) -> str:
    """Apply field updates to the YAML at `path`. -> "unchanged" | "edited" | "redumped"."""
    raw = path.read_bytes().decode("utf-8")
    eol = "\r\n" if "\r\n" in raw else "\n"
    text = raw.replace("\r\n", "\n")
    data = yaml.safe_load(text)
    want = _expected(data, upd_steps, upd_ovs)
    if want == data:
        return "unchanged"
    trailing = text.endswith("\n")
    lines = text.split("\n")
    if trailing:
        lines = lines[:-1]
    how = "edited"
    try:
        for sid, f in upd_steps.items():
            lines = _set_fields(lines, "steps", sid, f, _STEP_FIELDS)
        for oid, f in upd_ovs.items():
            lines = _set_fields(lines, "overlays", oid, f, ("clip",))
        new = "\n".join(lines) + ("\n" if trailing else "")
        ok = yaml.safe_load(new) == want
    except (KeyError, ValueError, AttributeError, TypeError, yaml.YAMLError):
        ok = False
    if not ok:
        how = "redumped"
        header = []
        for l in text.split("\n"):
            if l.startswith("#"):
                header.append(l)
            else:
                break
        body = yaml.safe_dump(want, sort_keys=False, allow_unicode=True, default_flow_style=False)
        new = "\n".join(header + [body.rstrip("\n")]) + "\n"
    path.write_bytes(new.replace("\n", eol).encode("utf-8"))
    return how


@dataclass
class ApplyResult:
    yaml_changed: list[str] = field(default_factory=list)      # "<path> (edited|redumped)"
    resumptions_written: list[Path] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def apply_plan(plan: PackPlan) -> ApplyResult:
    """Copy resumption clips to their convention paths and rewrite the YAML files.
    Call after every planned clip has been synthesized into `plan.out_dir`."""
    res = ApplyResult()
    for sp in plan.scenarios:
        res.kept += sp.kept
        res.warnings += sp.warnings
        for dst, src in sorted(sp.resumptions.items()):
            wrote = _copy_if_changed(src, dst)
            js, jd = src.with_suffix(".json"), dst.with_suffix(".json")
            if js.is_file():
                wrote = _copy_if_changed(js, jd) or wrote
            if wrote:
                res.resumptions_written.append(dst)
        base = sp.yaml_path.parent
        rel = lambda w: Path(os.path.relpath(w, base)).as_posix()
        upd_steps = {}
        for sid, p in sp.steps.items():
            on, end = onset_end_ms(p["wav"])
            upd_steps[sid] = {"clip": rel(p["wav"]), "speech_onset_ms": on, "speech_end_ms": end}
        upd_ovs = {oid: {"clip": rel(p["wav"])} for oid, p in sp.overlays.items()}
        how = rewrite_yaml(sp.yaml_path, upd_steps, upd_ovs)
        if how != "unchanged":
            res.yaml_changed.append(f"{sp.yaml_path} ({how})")
    return res
