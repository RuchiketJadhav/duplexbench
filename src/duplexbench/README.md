# duplexbench

Harness for the Dograh Duplex Bench (`docs/SPEC.md`; build plan `docs/BUILD_PLAN.md`).

## One call: the four accounted tasks (BUILD_PLAN §2.7)

`runner.run_call` owns four tasks, each spawned with `clock.task(...)` so the
VirtualClock can account for them. Every wait goes through the clock
(`sleep_until_ns` for time, `idle` for in-loop queues/events).

```
                         CallContext (run_id, call_id, scenario, clock, store, ...)
                                              |
  caller (CallerSource)                       |                       Adapter (system under test)
  scripted clips / simulator                  |                       hosted or self-hosted
        |                                     |                              ^        |
        | 20 ms frames + turn ends            |                              |        | events()
        v                                     |                              |        v
 +----------------+  channel.forward   +-------------+   send_frame   +------+   +-----------------+
 | _send_caller   |------------------->|  mu-law     |--------------->|      |   | _recv_agent     |
 | paces frames,  |  (effects, resample| 20 ms frames|                |      |   | adapter.safe_   |
 | ctx.turn_ended |   mu-law, GE loss) +-------------+                +------+   | events(ctx)     |
 +----------------+                                                              +-----------------+
        ^  inject(clip, level_db)                         audio -> channel.back -> playout.enqueue |
        |                                                 cancel_or_truncate -> playout.flush(t)   |
 +----------------+    onset / stop      +--------------------------+  tool_call -> tools.call     |
 | _overlays      |<---------------------| _play (playout.run)      |  -> adapter.tool_result      |
 | onset+offset,  |   (subscribe)        | one frame / frame_ms     |<-----------------------------+
 | not_injected   |                      | ear WAV, RMS, activity,  |
 +----------------+                      | queue_depth, backlog_ms  |
                                         +--------------------------+
                                                     |
       every task: store.put(...) (non-blocking) ---> writer thread ---> SQLite (WAL)

  End: E-END silence reached | call_timeout_ms | stall_ms | adapter closed | simulator <END>
  Any exception in any task -> `error` event, outcome transport_error, cancel the others,
  await store.flush(), close the WAV, return CallResult(outcome, usd_actual, flags).
```

## Contracts (BUILD_PLAN §2)

| Module | Contract |
|---|---|
| `clock.py` | `Clock` protocol, `RealClock`, `VirtualClock`, `DeadlockError`, `clock_info()` |
| `_loop.py` | precise selector loop (`run`, `new_loop`, `sleep_until_ns`), copied from asr-bench |
| `thresholds.py` | every [Proposed] number; `Thresholds.load()`, `.hash()`, `.meta(name)` |
| `storage.py` | `Store` writer thread, schema of §2.4, `canonical_json`, `config_id` |
| `context.py` | `CallContext`, `turn_ended(t_ns)` |
| `adapters/base.py` | `Adapter`, `AdapterEvent`, `AdapterCaps`, `EventKind`, `safe_events()` |
| `scenarios/` | `schema.py` (Pydantic), `loader.py` (`load_pack`, `validate`, `Pack`) |
| `metrics/names.py` | `Metric` (the only metric names), `.spec`, `.headline` |
| `metrics/base.py` | `Observation`, `CallRecord` |
| `budget.py` | `Budget`, `Reservation`, `BudgetExceeded` |
