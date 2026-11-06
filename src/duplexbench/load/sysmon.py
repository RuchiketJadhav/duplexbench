# Copied from C:\Users\Lenovo\research\asr-bench\src\asr_bench\load\sysmon.py (asr-bench, same author).
# Changed: imports (perf_counter_ns instead of asr-bench clock.now_ns; a local percentile),
# the loop-lag limit comes from Thresholds.loop_lag_p99_ms instead of a literal 5.0,
# and `JsonSink` (below) stands in for asr-bench's `system_metrics` table: duplex-bench's
# Store has no such table, so samples go to a JSON file next to the store (BUILD_PLAN M9-T04).
"""Protect the measurement: event-loop lag and machine load, recorded per second.

A 10 ms ticker measures how late the loop wakes it. If the loop is late, every
timestamp taken on it is late too, so a run whose loop-lag P99 exceeds
`th.loop_lag_p99_ms` is marked invalid. CPU above 80 % sustained is flagged.

SysMon runs on real time only (it is a RealClock instrument); under a
VirtualClock the load runner does not start it.
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from time import perf_counter_ns as now_ns
from typing import Any

import psutil

from duplexbench.thresholds import Thresholds

CPU_HOT_PCT = 80          # asr-bench machine-load flag, not a spec threshold


def percentile(xs: list[float], q: float) -> float | None:
    """Linear-interpolation percentile (numpy's default)."""
    s = sorted(xs)
    if not s:
        return None
    pos = (len(s) - 1) * q / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


class JsonSink:
    """`put(table, row)` collector written as one JSON file (stand-in for a sqlite table)."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.rows: dict[str, list[dict]] = {}

    def put(self, table: str, row: dict) -> None:
        self.rows.setdefault(table, []).append(dict(row))

    def write(self, summary: dict | None = None) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc: dict[str, Any] = {"samples": self.rows.get("system_metrics", [])}
        if summary is not None:
            doc["summary"] = summary
        self.path.write_text(json.dumps(doc, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        return self.path


class SysMon:
    def __init__(self, store, run_id: str, tick_ms: float = 10.0, th: Thresholds | None = None):
        self.store, self.run_id, self.tick_ns = store, run_id, int(tick_ms * 1e6)
        self.th = th or Thresholds.load()
        self.lags_ms: list[float] = []
        self.cpu: list[float] = []
        self.level: float = 0
        self.phase: str = ""
        self.active = 0
        self._task: asyncio.Task | None = None
        self._probe: asyncio.Task | None = None
        self._proc = psutil.Process()

    async def _run(self):
        psutil.cpu_percent(None)
        net0 = psutil.net_io_counters()
        sec_lags: list[float] = []
        next_t = now_ns() + self.tick_ns
        last_sample = now_ns()
        while True:
            await asyncio.sleep(max(0.0, (next_t - now_ns()) / 1e9))
            lag = (now_ns() - next_t) / 1e6
            sec_lags.append(lag)
            self.lags_ms.append(lag)
            next_t += self.tick_ns
            if now_ns() < next_t - self.tick_ns:   # never let the schedule run ahead
                next_t = now_ns() + self.tick_ns
            if now_ns() - last_sample >= 1_000_000_000 and (self._probe is None or self._probe.done()):
                last_sample = now_ns()
                self._probe = asyncio.create_task(self._sample(last_sample, sec_lags, net0))
                sec_lags = []

    async def _sample(self, t_ns: int, sec_lags: list, net0) -> None:
        """psutil calls can take 100+ ms on Windows: never on the event loop."""
        def probe():
            cpu = psutil.cpu_percent(None)
            net = psutil.net_io_counters()
            try:
                socks = len(self._proc.net_connections())
            except Exception:
                socks = -1
            return cpu, net, socks, self._proc.memory_info().rss / 2**20
        cpu, net, socks, mem = await asyncio.to_thread(probe)
        self.cpu.append(cpu)
        self.store.put("system_metrics", {
            "run_id": self.run_id, "t_ns": t_ns, "level": self.level, "phase": self.phase,
            "cpu_pct": cpu, "mem_mb": mem, "sockets": socks,
            "loop_lag_max_ms": max(sec_lags, default=0.0), "active_streams": self.active,
            "net_sent": net.bytes_sent - net0.bytes_sent,
            "net_recv": net.bytes_recv - net0.bytes_recv})

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> dict:
        if self._task:
            self._task.cancel()
        if self._probe:
            self._probe.cancel()
        p99 = percentile(self.lags_ms, 99) or 0.0
        hot = sum(1 for c in self.cpu if c > CPU_HOT_PCT) / max(1, len(self.cpu))
        return {"loop_lag_p99_ms": p99, "loop_lag_max_ms": max(self.lags_ms, default=0.0),
                "cpu_mean": sum(self.cpu) / max(1, len(self.cpu)),
                "cpu_over_80_frac": hot,
                "valid": p99 <= self.th.loop_lag_p99_ms, "cpu_flag": hot > 0.5}
