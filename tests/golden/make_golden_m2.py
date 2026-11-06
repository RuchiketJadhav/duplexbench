"""Golden M2 replay (BUILD_PLAN M2-T12, §4; CLAUDE.md M2 "Accept (golden M2)"; decision D3).

Fixture scenario `track_a_002` (Y-INT, H-BC, P-PAUSE, wait-class Y-CMD, E-END) and its
control twin, under the VirtualClock, MockAgent `stops` (440 Hz tone, tool calls off),
channel seeds fwd 11 / back 12, conditions C0 and C1 (4 calls: overlay + twin each).
The calls go through runner.run_call with the overlay engine, then `cli score`'s
`score_run` writes the observations. Frozen: the `overlays` rows and the Track A
observation table (call, metric, event, variant, value, valid, tags).

Clips: the fixture tones. s1's resumption and the Y-CMD's resumption reuse
`a002_s2.wav` (the fixture ships no resumption clip; tones carry no words).

    uv run python tests/golden/make_golden_m2.py     # rewrites expected_m2.json

Regenerate only with an intentional change to the overlay engine, scripted caller,
floor-control metrics, runner, playout, channel, clock or mock agent, and say why in
docs/worklog.md.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import yaml

from duplexbench.adapters.mock_agent import MockAgent, MockProgram
from duplexbench.cli_score import score_run
from duplexbench.clock import VirtualClock
from duplexbench.overlays.scripted_run import read_pcm, run_scripted_call
from duplexbench.scenarios.schema import Scenario
from duplexbench.storage import Store, canonical_json
from duplexbench.thresholds import Thresholds

EXPECTED = Path(__file__).with_name("expected_m2.json")
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "scenarios" / "public-dev"
RUN_ID = "golden_m2"
CONDITIONS = ("C0", "C1")
SEED_FWD, SEED_BACK = 11, 12
TRACK_A = {"yield_rate", "t_stop_ear", "talk_over_share", "respond_resume", "t_respond_ear",
           "wait_compliance", "hold_speaking_rate", "content_leakage_rate", "hold_listening_rate",
           "response_rate", "t_turn_gap_ear"}


def scenario() -> Scenario:
    return Scenario.model_validate(yaml.safe_load((FIX / "track_a_002.yaml").read_text(encoding="utf-8")))


def clips() -> dict:
    a = FIX / "audio"
    return {"s1": read_pcm(a / "a002_s1.wav"), "s1/resumption": read_pcm(a / "a002_s2.wav"),
            "s2": read_pcm(a / "a002_s2.wav"), "o1": read_pcm(a / "a002_yint.wav"),
            "o2": read_pcm(a / "a002_bc.wav"), "o4": read_pcm(a / "a002_bc.wav"),
            "o4/resumption": read_pcm(a / "a002_s2.wav")}


def run_calls(out_dir: Path, th: Thresholds, behaviour: str = "stops",
              conditions: tuple[str, ...] = CONDITIONS) -> Path:
    """Run overlay + control twin per condition into out_dir/events.sqlite; return its path."""
    sc, cl = scenario(), clips()
    db = Path(out_dir) / "events.sqlite"
    store = Store(db)
    try:
        store.put("runs", {"run_id": RUN_ID, "started_ns": 0,
                           "config_json": canonical_json({"scenarios": {sc.id: sc.model_dump(mode="json")},
                                                          "behaviour": behaviour}),
                           "hashes_json": canonical_json({"thresholds": th.hash()}),
                           "calibration_json": "{}", "run_card_json": "{}"})

        async def main(clock):
            for cond in conditions:
                for ctl in (False, True):
                    agent = MockAgent(clock, MockProgram(behaviour=behaviour, tool_at_ms=None), th)
                    await run_scripted_call(clock, store, sc, cl, agent, th, Path(out_dir),
                                            run_id=RUN_ID, condition=cond, is_control=ctl,
                                            system_id="mock", seed_fwd=SEED_FWD, seed_back=SEED_BACK)

        clock = VirtualClock()
        asyncio.run(clock.run(main(clock)))
        store.flush_sync()
    finally:
        store.close()
    return db


def _variant(tags: tuple[str, ...]) -> str:
    return ",".join(t for t in tags if t.startswith("@"))


def generate(out_dir: Path) -> dict:
    import sqlite3
    th = Thresholds()           # spec defaults, independent of configs/thresholds.yaml edits
    db = run_calls(Path(out_dir), th)
    obs = score_run(db, RUN_ID, th, scenarios={scenario().id: scenario()})
    table = [[o.call_id, o.metric, o.event_id, _variant(o.tags), o.value, o.valid,
              [t for t in o.tags if not t.startswith("@")]]
             for o in obs if o.metric in TRACK_A]
    conn = sqlite3.connect(str(db))
    try:
        ovs = [list(r) for r in conn.execute(
            "SELECT call_id, overlay_id, cls, scheduled_ns, actual_ns, onset_ns, end_ns, tags_json "
            "FROM overlays ORDER BY call_id, overlay_id")]
        outcomes = [list(r) for r in conn.execute("SELECT call_id, outcome FROM calls ORDER BY call_id")]
    finally:
        conn.close()
    return {"config": {"scenario": "track_a_002", "conditions": list(CONDITIONS),
                       "seed_fwd": SEED_FWD, "seed_back": SEED_BACK, "behaviour": "stops",
                       "thresholds_hash": th.hash()},
            "outcomes": outcomes, "overlays": ovs, "track_a": table}


def main() -> None:
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        a, b = generate(Path(d1)), generate(Path(d2))
    if a != b:
        sys.exit("two regenerations differ: the replay is not deterministic")
    EXPECTED.write_text(json.dumps(a, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {EXPECTED} ({len(a['track_a'])} Track A observations)")


if __name__ == "__main__":
    main()
