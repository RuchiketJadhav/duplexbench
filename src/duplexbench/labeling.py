"""Terminal labeling tool for classifier validation sets (BUILD_PLAN M2b-T03, M7b; spec §4.1, §8).

    manifest.jsonl  (one item per line)                 results/labels/<set>.jsonl (append-only)
    {"item_id", "text", "wav"?, "t0_ms"?, "t1_ms"?,  -> {"item_id", "set", "label", "labeler_id",
     "clip"?, "events"?, "anchors"?, "context"?}         "t_utc"}

    for each item without a label from this labeler and with < LABELERS_PER_ITEM labelers:
        cut the clip (report.clips.cut) when the item gives wav + t0/t1, else use "clip"
        player(clip_path)           pluggable; default prints the path (no audio device)
        print the reference-ASR text
        read a label: a choice | "r" replay | "s" skip | "q" quit

Every label is appended and flushed at once, so a session resumes where it
stopped: items this labeler already labeled are skipped. Two labelers per item
(spec §8 double-labeled sets); a third labeler is not offered an item that
already has two.
"""
from __future__ import annotations

import datetime as _dt
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from duplexbench.preregister import RESULTS_DIR

LABEL_SETS: dict[str, tuple[str, ...]] = {
    "respond_resume": ("respond", "resume", "uncertain"),     # spec §4.1
    "success_claim": ("claim", "no_claim"),                    # spec §4.2, §8
    "recovery_request": ("request", "no_request"),             # spec §4.3, §8
}
LABELERS_PER_ITEM = 2
LABELS_DIR = RESULTS_DIR / "labels"


def default_player(clip_path: Path | str) -> None:
    print(f"[clip] {clip_path}")


def load_manifest(path: Path | str) -> list[dict[str, Any]]:
    items = []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            d = json.loads(line)
            if "item_id" not in d:
                raise ValueError(f"{path}:{i}: item without item_id")
            items.append(d)
    if len({d["item_id"] for d in items}) != len(items):
        raise ValueError(f"{path}: duplicate item_id")
    return items


def read_labels(out_path: Path) -> list[dict[str, Any]]:
    if not out_path.exists():
        return []
    return [json.loads(x) for x in out_path.read_text(encoding="utf-8").splitlines() if x.strip()]


def coverage(out_path: Path) -> dict[str, set[str]]:
    """item_id -> labeler ids that labeled it."""
    cov: dict[str, set[str]] = defaultdict(set)
    for r in read_labels(out_path):
        cov[r["item_id"]].add(r["labeler_id"])
    return dict(cov)


def label_path(set_name: str, out_dir: Path = LABELS_DIR) -> Path:
    return Path(out_dir) / f"{set_name}.jsonl"


def _clip_for(item: dict[str, Any], clip_dir: Path | None) -> Path | None:
    if item.get("wav") and item.get("t0_ms") is not None and item.get("t1_ms") is not None:
        from duplexbench.report.clips import cut
        clip, _ = cut(item["wav"], item["t0_ms"], item["t1_ms"], item.get("events", []),
                      out_dir=clip_dir)
        return clip
    return Path(item["clip"]) if item.get("clip") else None


def run_session(manifest: Path | str, set_name: str, labeler_id: str, *,
                out_dir: Path | str = LABELS_DIR, player: Callable[[Path], None] = default_player,
                input_fn: Callable[[str], str] = input, output_fn: Callable[[str], None] = print,
                clip_dir: Path | str | None = None) -> int:
    """Label every remaining item for `labeler_id`; return how many labels were written this session."""
    if set_name not in LABEL_SETS:
        raise ValueError(f"unknown label set {set_name!r}; one of {sorted(LABEL_SETS)}")
    if not labeler_id.strip():
        raise ValueError("labeler_id is required")
    choices = LABEL_SETS[set_name]
    items = load_manifest(manifest)
    out = label_path(set_name, Path(out_dir))
    out.parent.mkdir(parents=True, exist_ok=True)
    cov = coverage(out)
    todo = [(i, it) for i, it in enumerate(items, start=1)
            if labeler_id not in cov.get(it["item_id"], set())
            and len(cov.get(it["item_id"], set())) < LABELERS_PER_ITEM]
    output_fn(f"set {set_name}: {len(items)} item(s), {len(todo)} left for {labeler_id}")
    keys = "/".join(choices)
    written = 0
    with out.open("a", encoding="utf-8") as fh:
        for k, it in todo:
            clip = _clip_for(it, Path(clip_dir) if clip_dir else None)
            output_fn(f"--- item {k}/{len(items)}: {it['item_id']}")
            if it.get("context"):
                output_fn(f"context: {it['context']}")
            output_fn(f"text: {it.get('text', '')}")
            if it.get("anchors"):
                output_fn(f"anchors: {', '.join(it['anchors'])}")
            if clip is not None:
                player(clip)
            while True:
                ans = input_fn(f"label [{keys}] (r replay, s skip, q quit): ").strip().lower()
                if ans == "r" and clip is not None:
                    player(clip)
                    continue
                if ans in ("s", "q") or ans in choices:
                    break
                output_fn(f"  not one of {keys}")
            if ans == "q":
                output_fn(f"stopped; {written} label(s) written this session")
                return written
            if ans == "s":
                continue
            rec = {"item_id": it["item_id"], "set": set_name, "label": ans, "labeler_id": labeler_id,
                   "t_utc": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")}
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
            fh.flush()
            written += 1
    output_fn(f"done; {written} label(s) written this session")
    return written
