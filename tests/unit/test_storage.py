"""BUILD_PLAN §2.4: writer-thread store, schema, canonical_json, config_id."""
import asyncio
import sqlite3

import pytest

from duplexbench.storage import TABLES, Store, canonical_json, config_id, connect


def test_put_flush_roundtrip(store):
    for i in range(250):     # more than two batches of 100
        store.put("events", {"run_id": "r", "call_id": "c", "t_ns": i * 20, "seq": i,
                             "kind": "audio", "src": "agent", "payload_json": "{}"})
    asyncio.run(store.flush())
    assert store.written("events") == 250
    conn = sqlite3.connect(store.path)
    n = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    last = conn.execute("SELECT max(seq) FROM events").fetchone()[0]
    conn.close()
    assert (n, last) == (250, 249)


def test_flush_sync(store):
    store.put("runs", {"run_id": "r1", "started_ns": 0, "config_json": "{}",
                       "hashes_json": "{}", "calibration_json": None, "run_card_json": None})
    store.flush_sync()
    assert store.written("runs") == 1


def test_unknown_table_rejected(store):
    with pytest.raises(ValueError):
        store.put("nope", {"a": 1})


def test_schema_tables_and_indexes(tmp_path):
    conn = connect(tmp_path / "s.sqlite")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    idx = {r[0]: r[1] for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert set(TABLES) == {"runs", "calls", "events", "playout", "overlays", "tool_calls",
                           "sim_turns", "observations"}
    assert set(TABLES) <= names
    assert "(call_id, t_ns, seq)" in idx["idx_events_call"]
    assert "(call_id, t_ns)" in idx["idx_playout_call"]
    assert mode == "wal"


def test_columns_match_plan(tmp_path):
    conn = connect(tmp_path / "s.sqlite")
    cols = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in TABLES}
    conn.close()
    assert cols["playout"] == ["run_id", "call_id", "t_ns", "frame_idx", "rms_dbfs", "active",
                               "queue_depth", "backlog_ms", "flushed"]
    assert cols["observations"] == ["run_id", "call_id", "metric", "scenario_id", "trial",
                                    "condition", "system_id", "event_id", "value", "valid",
                                    "tags_json"]
    for t in TABLES:
        if t != "runs":
            assert {"run_id", "call_id"} <= set(cols[t]), t


def test_config_id_stable_across_key_order():
    a = {"model": "x", "vad": {"silence_ms": 500, "threshold": 0.5}, "voice": "alloy"}
    b = {"voice": "alloy", "vad": {"threshold": 0.5, "silence_ms": 500}, "model": "x"}
    assert canonical_json(a) == canonical_json(b)
    assert config_id(a) == config_id(b)
    assert len(config_id(a)) == 16
    assert config_id(a) != config_id({**a, "voice": "echo"})


def test_close_is_idempotent(tmp_path):
    s = Store(tmp_path / "x.sqlite")
    s.close()
    s.close()
