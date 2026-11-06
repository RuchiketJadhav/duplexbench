"""load/sysmon.py (copied from asr-bench; no sysmon test existed there): BUILD_PLAN M9-T04."""
from __future__ import annotations

import asyncio
import json

from duplexbench.load.sysmon import JsonSink, SysMon, percentile
from duplexbench.thresholds import Thresholds


def test_percentile_linear():
    assert percentile([], 99) is None
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0
    assert abs(percentile([float(i) for i in range(101)], 99) - 99.0) < 1e-9


def test_sysmon_samples_to_json(tmp_path):
    sink = JsonSink(tmp_path / "sysmon_r.json")

    async def main():
        mon = SysMon(sink, "r", tick_ms=5.0, th=Thresholds())
        mon.level, mon.phase = 4, "sustain"
        mon.start()
        await asyncio.sleep(1.3)
        await asyncio.sleep(0.2)          # let the first probe (thread) finish
        return await mon.stop()

    summary = asyncio.run(main())
    assert set(summary) >= {"loop_lag_p99_ms", "loop_lag_max_ms", "valid", "cpu_flag"}
    assert summary["valid"] == (summary["loop_lag_p99_ms"] <= Thresholds().loop_lag_p99_ms)
    p = sink.write(summary)
    doc = json.loads(p.read_text(encoding="utf-8"))
    assert doc["summary"]["loop_lag_p99_ms"] == summary["loop_lag_p99_ms"]
    assert len(doc["samples"]) >= 1
    s = doc["samples"][0]
    assert s["run_id"] == "r" and s["level"] == 4 and s["phase"] == "sustain"
