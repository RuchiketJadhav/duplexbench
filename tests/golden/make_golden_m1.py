"""Golden M1 replay (BUILD_PLAN M1-T16, §4; spec §1.1 "a mock run reproduces byte-for-byte").

One scripted mock call under the VirtualClock: condition C1, channel seeds
fwd 11 / back 12, MockAgent `stops` (440 Hz tone voice, 24 kHz provider audio),
caller turn 0-1 s, a 300 ms interruption at 2.16 s, 5 s call. Frozen: the WAV
sha256, every non-audio event (t_ns, seq, kind, src, payload), the audio event
count, a sha256 over the full event log (audio included), the playout row count
and a sha256 over the playout rows.

    uv run python tests/golden/make_golden_m1.py     # rewrites expected_m1.json

Regenerate only with an intentional change to playout, channel, clock, runner,
mock agent or recorder (BUILD_PLAN §4), and say why in docs/worklog.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from duplexbench.adapters.mock_agent import MockProgram
from duplexbench.calibrate import CalibConfig, simulate_call
from duplexbench.clock import VirtualClock
from duplexbench.storage import canonical_json
from duplexbench.thresholds import Thresholds

EXPECTED = Path(__file__).with_name("expected_m1.json")
CONFIG = CalibConfig(calls=1, condition="C1", seed_fwd=11, seed_back=12, call_ms=5_000,
                     turn_ms=1_000, interrupt_at_ms=2_160, interrupt_ms=300,
                     caller_level_dbfs=-20.0, mock=MockProgram(behaviour="stops"))


def generate(out_dir: Path) -> dict:
    th = Thresholds()           # spec defaults, independent of configs/thresholds.yaml edits
    clock = VirtualClock()
    sim = asyncio.run(clock.run(simulate_call(clock, CONFIG, th, Path(out_dir), "golden_m1",
                                              run_id="golden_m1")))
    rows = sim.store.rows
    events = sorted(rows["events"], key=lambda r: (r["t_ns"], r["seq"]))
    ev = [[r["t_ns"], r["seq"], r["kind"], r["src"], r["payload_json"]] for r in events]
    playout = [[r["t_ns"], r["frame_idx"], r["rms_dbfs"], r["active"], r["queue_depth"],
                r["backlog_ms"], r["flushed"]] for r in rows["playout"]]
    return {
        "config": {"condition": CONFIG.condition, "seed_fwd": CONFIG.seed_fwd,
                   "seed_back": CONFIG.seed_back, "behaviour": CONFIG.mock.behaviour,
                   "thresholds_hash": th.hash()},
        "outcome": sim.result.outcome,
        "wav_sha256": hashlib.sha256(sim.wav_path.read_bytes()).hexdigest(),
        "events": [e for e in ev if e[2] != "audio"],
        "audio_events": sum(e[2] == "audio" for e in ev),
        "events_sha256": hashlib.sha256(canonical_json(ev).encode()).hexdigest(),
        "playout_rows": len(playout),
        "playout_sha256": hashlib.sha256(canonical_json(playout).encode()).hexdigest(),
        "overlays": [[r["overlay_id"], r["onset_ns"], r["end_ns"]] for r in rows["overlays"]],
    }


def main() -> None:
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        a, b = generate(Path(d1)), generate(Path(d2))
    if a != b:
        sys.exit("two regenerations differ: the replay is not deterministic")
    EXPECTED.write_text(json.dumps(a, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {EXPECTED} (wav {a['wav_sha256'][:16]}, events {a['events_sha256'][:16]})")


if __name__ == "__main__":
    main()
