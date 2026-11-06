# $0 mock smoke log (integration pass, 2026-11-05)

End-to-end `doctor -> calibrate -> preregister -> run -> score -> report -> verify -> gate`
against the mock agent (`configs/systems/mock_stops.yaml`) on the smoke pack
(`tests/fixtures/scenarios/smoke`, 2 Track A calls, C0). No provider was called; cost $0.
Host: the Windows laptop (not the cloud instance), so calibration numbers are laptop numbers.
Each block is the command's stdout+stderr followed by its exit code.

Run id: `smoke_mock-20261105T043240Z` (results/smoke_mock-20261105T043240Z/, gitignored). An earlier run of the same config,
`smoke_mock-20261105T043032Z`, is also kept: it was made before `score`'s default
`--db` was fixed (it looked in results/events.sqlite) and before `planned_calls` /
`coverage` entered `runs.config_json`; its second call had a 66 ms pacing slip p99
(laptop scheduling), so its gate also failed `harness_invalid_share` (1/2 pacing_fail).

Summary:
- calibrate: PASS (all timing, loop-lag, pacing and Silero checks; refasr stage `skipped`:
  no reference ASR is pinned, so Track C runs are refused (D4) until one is).
- run: 2/2 `completed`, projected and actual 0.0000 USD.
- verify: 0 differences.
- gate: FAIL, as expected for a 2-call smoke. Failing checks:
  - `run_card_complete`: refasr, simulator, network_baseline, iteration_counts and
    vendors_audio are not filled for a mock run (no reference ASR, no simulator, no
    `netcheck` / `card-fill`, no vendor receives audio), the mock system has
    `reasoning_effort` / `turn_detection` null, and the calibration record has no
    reference-ASR RTF (`calibration.rtf`) because the stage was skipped.
  - `coverage_n_met`: the smoke run config pre-registers no `coverage:` block (coverage n
    is a human pre-registration decision).
  - Passing but only nominally: `power_notes` prints that n=2 is below the spec §9.2
    minimum for p50/p90/p95.

## 1. doctor

```
$ uv run duplexbench doctor
python: 3.12.6 (Windows-11-10.0.22621-SP0)
clock: {'name': 'perf_counter', 'implementation': 'QueryPerformanceCounter()', 'resolution_s': 1e-07, 'monotonic': True}
DUPLEX_PRIVATE_DIR: not set
thresholds: C:\Users\Lenovo\research\duplex-bench\configs\thresholds.yaml
thresholds hash: 394923c1104b32dd13b94068cc42f8f51f94dc7c87cd433be89c1555abd5a73b
exit 0
```

## 2. calibrate (RealClock, this laptop)

```
$ uv run duplexbench calibrate
calibration  condition=C0  calls=20  clock=QueryPerformanceCounter()
timing          kind      n   err p50  |err| p95  |err| max    tol  ok
t_ttfa_recv     event    20      0.67       0.96       1.39      5  PASS
t_stop_model    event    20      0.64       1.89       1.96      5  PASS
t_tool_offset   event    20      0.57       1.52       1.81      5  PASS
t_turn_gap_ear  audio    20     74.95      75.36      75.44     80  PASS
t_stop_ear      audio    20      8.79       9.11       9.26     80  PASS
loop_lag_p99_ms                             2.97  < 5  PASS
pacing_slip_p99_ms                          3.85  < 10  PASS
silero_disagreement_ms                      6.00  < 40  PASS
refasr                                   skipped  (no reference ASR configured (M4-T01: pin...)
failed calls: 0   RESULT: PASS
written: C:\Users\Lenovo\research\duplex-bench\results\calibration-20261105T042836Z.json
exit 0
```

## 3. preregister the smoke pack

```
$ uv run duplexbench preregister --pack-dir tests/fixtures/scenarios/smoke
systems: {'dograh_stack': '511e82d74b1aa203', 'gemini_live': '702b1481240e8cf4', 'gpt_live': '0e47ff14bbefce24', 'grok_voice': '69e8e1cdeeae5fc7', 'mock_stops': '68cb77066425cca8', 'openai_realtime': 'dd1464cdec57a03e', 'pipecat_cascade': '4abe8bce7abc735f', 'selfhosted_moshi': '0e7ca0da40e5d35c'}
pack: 8e77e63ae9afd18f22d4e76d55fec987bef51d964e15a8437b6837842dcf826f
langpack: a0e2dc74343c24b8ac0f826da9f391ea7cb24ccd0555f55f4f3d7a4dcd570333
thresholds: 394923c1104b32dd13b94068cc42f8f51f94dc7c87cd433be89c1555abd5a73b
private_tree: unmounted
harness_commit: unknown
wrote C:\Users\Lenovo\research\duplex-bench\results\preregister-20261105T043023877792Z.json
exit 0
```

## 4a. run without --yes (projection only)

```
$ uv run duplexbench run configs/runs/smoke_mock.yaml
run smoke_mock: 2 scenarios x 1 conditions x 1 systems = 2 calls
pack 8e77e63ae9af  thresholds 394923c1104b
preregister: ok (2026-11-05T04:30:23.877792Z)
calibration: pass (calibration-20261105T042836Z.json, 0.0 h old)
projection (calls x est. minutes x USD/min):
  mock_stops: 2 calls x 0.23 min total x 0.0000 USD/min = 0.0000 USD  [unpriced: free]
  total projected: 0.0000 USD
not started: re-run with --yes to spend the projected amount
exit 1
```

## 4. run

```
$ uv run duplexbench run configs/runs/smoke_mock.yaml --yes
run smoke_mock: 2 scenarios x 1 conditions x 1 systems = 2 calls
pack 8e77e63ae9af  thresholds 394923c1104b
preregister: ok (2026-11-05T04:30:23.877792Z)
calibration: pass (calibration-20261105T042836Z.json, 0.04 h old)
projection (calls x est. minutes x USD/min):
  mock_stops: 2 calls x 0.23 min total x 0.0000 USD/min = 0.0000 USD  [unpriced: free]
  total projected: 0.0000 USD
run_id smoke_mock-20261105T043240Z -> C:\Users\Lenovo\research\duplex-bench\results\smoke_mock-20261105T043240Z
mock_stops-smoke_a_001-C0-t0: completed  projected 0.0000 USD  actual 0.0000 USD
mock_stops-smoke_a_002-C0-t0: completed  projected 0.0000 USD  actual 0.0000 USD
done: {'completed': 2}  actual 0.0000 USD
exit 0
```

## 5. score

```
$ uv run duplexbench score smoke_mock-20261105T043240Z
note: no reference-ASR words yet (M4): Track C / D observers get an empty transcript
scored 2 call(s), 10 observation(s)
run smoke_mock-20261105T043240Z: 10 observation(s), 4 metric(s)
metric              system      variant  n  n_all      mean       p50  CI          tags
------------------  ----------  -------  -  -----  --------  --------  ----------  ------------
t_turn_gap_ear      mock_stops  -        2      2  477.2894  477.2894  (CI in M7)  -
dead_air_in_tool_s  mock_stops  -        2      2    0.0000    0.0000  (CI in M7)  -
t_ttfa_recv         mock_stops  -        2      2  401.9725  401.9725  (CI in M7)  -
t_dead_air          mock_stops  -        4      4    4.1200    4.1200  (CI in M7)  diagnostic:2
exit 0
```

## 6. report

```
$ uv run duplexbench report smoke_mock-20261105T043240Z
report: results\smoke_mock-20261105T043240Z\report\index.html
exit 0
```

## 7. verify

```
$ uv run duplexbench verify smoke_mock-20261105T043240Z results/smoke_mock-20261105T043240Z/report
verify: 0 difference(s)
exit 0
```

## 8. gate

```
$ uv run duplexbench gate smoke_mock-20261105T043240Z
gate smoke_mock-20261105T043240Z: FAIL
  [FAIL] run_card_complete: missing: refasr, simulator, network_baseline, iteration_counts, vendors_audio, systems.mock_stops.reasoning_effort, systems.mock_stops.turn_detection, calibration.rtf
  [ok] headline_n_ci: 1 headline rows
  [ok] power_notes: 1 notes
  [ok] calibration_passed: passed
  [ok] harness_invalid_share: 0/2 = 0.0 (max 0.02) {}
  [ok] attempts_preserved: 2 calls stored
  [ok] exclusion_rates: reported for ['mock_stops']
  [FAIL] coverage_n_met: no pre-registered coverage in run config
power notes (planner, spec 9.2):
  t_turn_gap_ear [mock_stops, C0]: n=2; minimum n p50 30, p90 100, p95 200; below: p50, p90, p95
exclusion rates per system (expected outcomes, never failures):
  mock_stops: ambiguous_end 0/6, not_injected 0/6, unmatched_control 0/6, contested 0/6
exit 1
```
