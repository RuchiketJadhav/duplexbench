"""Stereo clip cutting with an event timeline (BUILD_PLAN M7-T04 `report/clips.py`, used by `cli label`; spec §10 item 9).

    results/<run_id>/<call_id>.wav  (L = caller as delivered, R = ear; BUILD_PLAN §2.2)
        └── cut(wav, t0_ms, t1_ms, events) ──► <out_dir>/<stem>_<t0>-<t1>.wav
                                               <out_dir>/<stem>_<t0>-<t1>.json  (timeline)

Times are milliseconds from the recording's first sample. Events carry clock
`t_ns`; `origin_ns` is the clock time of the first sample (default 0), so an
event at `t_ns` sits at `(t_ns - origin_ns) / 1e6` ms in the file. The timeline
lists the events inside [t0, t1] with `t_ms` relative to the clip start.
The window is clamped to the file; an empty window raises.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import soundfile as sf

from duplexbench.metrics.base import Event


def _event_dict(ev: Event | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(ev, Event):
        return {"t_ns": ev.t_ns, "seq": ev.seq, "kind": ev.kind, "src": ev.src, "payload": ev.payload}
    d = dict(ev)
    d.setdefault("seq", 0)
    return d


def cut(wav_path: Path | str, t0_ms: float, t1_ms: float,
        events: Iterable[Event | Mapping[str, Any]] = (), *, out_dir: Path | str | None = None,
        origin_ns: int = 0) -> tuple[Path, str]:
    """Cut [t0_ms, t1_ms) out of a (stereo) WAV; return `(clip_path, timeline_json)`.

    The clip keeps every channel, the sample rate and the subtype of the source.
    `timeline_json` is also written next to the clip as `<clip>.json`.
    `out_dir` defaults to `<wav dir>/clips`.
    """
    src = Path(wav_path)
    info = sf.info(str(src))
    rate = info.samplerate
    dur_ms = info.frames * 1e3 / rate
    a_ms, b_ms = max(0.0, float(t0_ms)), min(dur_ms, float(t1_ms))
    if b_ms <= a_ms:
        raise ValueError(f"empty clip window [{t0_ms}, {t1_ms}) ms in {src} ({dur_ms:.1f} ms long)")
    a, b = int(round(a_ms * rate / 1e3)), int(round(b_ms * rate / 1e3))
    data, _ = sf.read(str(src), start=a, stop=b, dtype="int16", always_2d=True)

    out = Path(out_dir) if out_dir is not None else src.parent / "clips"
    out.mkdir(parents=True, exist_ok=True)
    clip = out / f"{src.stem}_{int(a_ms)}-{int(b_ms)}.wav"
    sf.write(str(clip), data, rate, subtype=info.subtype)

    evs = []
    for e in events:
        d = _event_dict(e)
        t_ms = (int(d["t_ns"]) - origin_ns) / 1e6
        if a_ms <= t_ms <= b_ms:
            evs.append({"t_ms": round(t_ms - a_ms, 3), "kind": d.get("kind"), "src": d.get("src"),
                        "seq": d["seq"], "payload": d.get("payload", {})})
    evs.sort(key=lambda d: (d["t_ms"], d["seq"]))
    timeline = json.dumps({"source": src.name, "t0_ms": a_ms, "t1_ms": b_ms, "rate": rate,
                           "channels": int(data.shape[1]), "channel_names": _names(data.shape[1]),
                           "events": evs}, sort_keys=True, indent=1, default=str)
    clip.with_suffix(".json").write_text(timeline, encoding="utf-8")
    return clip, timeline


def _names(n: int) -> list[str]:
    return ["caller", "ear"] if n == 2 else [f"ch{i}" for i in range(n)]
