# Writer-thread design, canonical_json, config_id, connect and Store are copied from
# C:\Users\Lenovo\research\asr-bench\src\asr_bench\storage.py (asr-bench, same author).
# Changed: the schema (BUILD_PLAN §2.4), the default batch size (100) and the thread name.
"""SQLite event log, and the writer thread that keeps disk off the event loop (spec §1.1, §10 item 1).

    event loop (timing)          writer thread
    ------------------           -------------
    store.put(table, row)  --->  queue.Queue  ---> executemany, batches of 100, WAL
         (non-blocking)                             commit every batch
    await store.flush()    --->  waits until the queue is drained

Under a VirtualClock, `await store.flush()` directly (not through `clock.idle`):
the flush completes on a thread, so virtual time must hold still while it runs.

Identity:
    config_id = sha256(canonical JSON of a resolved system config)[:16]

Tables follow BUILD_PLAN §2.4. `observations` is written by `cli score`, never
by the live loop.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import queue
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started_ns INTEGER, config_json TEXT, hashes_json TEXT,
  calibration_json TEXT, run_card_json TEXT
);
CREATE TABLE IF NOT EXISTS calls (
  call_id TEXT PRIMARY KEY, run_id TEXT, scenario_id TEXT, trial INTEGER, condition TEXT,
  system_id TEXT, is_control INTEGER, outcome TEXT, started_ns INTEGER, ended_ns INTEGER,
  wav_path TEXT, usd_projected REAL, usd_actual REAL, flags_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT, call_id TEXT, t_ns INTEGER, seq INTEGER, kind TEXT, src TEXT, payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_call ON events (call_id, t_ns, seq);
CREATE TABLE IF NOT EXISTS playout (
  run_id TEXT, call_id TEXT, t_ns INTEGER, frame_idx INTEGER, rms_dbfs REAL, active INTEGER,
  queue_depth INTEGER, backlog_ms REAL, flushed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_playout_call ON playout (call_id, t_ns);
CREATE TABLE IF NOT EXISTS overlays (
  run_id TEXT, call_id TEXT, overlay_id TEXT, cls TEXT, scheduled_ns INTEGER, actual_ns INTEGER,
  onset_ns INTEGER, end_ns INTEGER, tags_json TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
  run_id TEXT, call_id TEXT, tool_call_id TEXT, name TEXT, args_json TEXT, start_ns INTEGER,
  result_ns INTEGER, injected_latency_ms INTEGER, injected_failure TEXT, result_json TEXT
);
CREATE TABLE IF NOT EXISTS sim_turns (
  run_id TEXT, call_id TEXT, turn_idx INTEGER, text TEXT, tts_words_json TEXT, send_ns INTEGER,
  think_ms REAL, speculative INTEGER, discarded INTEGER, late_ms REAL, prompt_hash TEXT
);
CREATE TABLE IF NOT EXISTS observations (
  run_id TEXT, call_id TEXT, metric TEXT, scenario_id TEXT, trial INTEGER, condition TEXT,
  system_id TEXT, event_id TEXT, value REAL, valid INTEGER, tags_json TEXT
);
"""

TABLES = ("runs", "calls", "events", "playout", "overlays", "tool_calls", "sim_turns",
          "observations")


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def config_id(model_config: dict) -> str:
    """Immutable identity of a fully resolved system config (spec §10 item 1)."""
    return hashlib.sha256(canonical_json(model_config).encode()).hexdigest()[:16]


def gz(obj: Any) -> bytes:
    return gzip.compress(canonical_json(obj).encode())


def ungz(blob: bytes | None) -> Any:
    return None if blob is None else json.loads(gzip.decompress(blob))


def connect(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


class Store:
    """Write-behind store. `put` never blocks the event loop on disk."""

    _STOP = object()

    def __init__(self, path: Path, batch: int = 100):
        self.path = Path(path)
        self.batch = batch
        self._q: queue.Queue = queue.Queue()
        self._written: dict[str, int] = {}
        self._error: BaseException | None = None
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="duplexbench-writer", daemon=True)
        self._thread.start()

    def put(self, table: str, row: dict) -> None:
        if self._error:
            raise RuntimeError("writer thread failed") from self._error
        if table not in TABLES:
            raise ValueError(f"unknown table {table!r}")
        self._q.put((table, row))

    def _run(self) -> None:
        conn = connect(self.path)
        try:
            while True:
                item = self._q.get()
                items = [item]
                while len(items) < self.batch:
                    try:
                        items.append(self._q.get_nowait())
                    except queue.Empty:
                        break
                stop = any(i is self._STOP for i in items)
                rows = [i for i in items if i is not self._STOP and not isinstance(i, threading.Event)]
                by_table: dict[tuple, list] = {}
                for table, row in rows:
                    by_table.setdefault((table, tuple(row)), []).append(tuple(row.values()))
                for (table, cols), values in by_table.items():
                    sql = (f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
                           f"VALUES ({','.join('?' * len(cols))})")
                    conn.executemany(sql, values)
                    self._written[table] = self._written.get(table, 0) + len(values)
                conn.commit()
                for i in items:
                    if isinstance(i, threading.Event):
                        i.set()
                for _ in items:
                    self._q.task_done()
                if stop:
                    return
        except BaseException as e:   # surfaced on the next put/flush
            self._error = e
            # release any flush waiters so they see the error instead of timing out
            while True:
                try:
                    i = self._q.get_nowait()
                except queue.Empty:
                    break
                if isinstance(i, threading.Event):
                    i.set()
        finally:
            conn.close()

    def flush_sync(self, timeout: float = 60.0) -> None:
        ev = threading.Event()
        self._q.put(ev)
        if not ev.wait(timeout):
            raise TimeoutError("writer did not flush")
        if self._error:
            raise RuntimeError("writer thread failed") from self._error

    async def flush(self, timeout: float = 60.0) -> None:
        await asyncio.to_thread(self.flush_sync, timeout)

    def written(self, table: str) -> int:
        return self._written.get(table, 0)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._q.put(self._STOP)
        self._thread.join(timeout=60)  # threshold-ok writer join timeout in s, not a spec number
        if self._error:
            raise RuntimeError("writer thread failed") from self._error
