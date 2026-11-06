# Runbook: the Core run (M8) and the phone/load pass (M9)

This is the procedure for a release-gate run (CLAUDE.md M8, spec §9.3 Core): Dograh's
stack and one reference speech-to-speech system on the whole `private-test` pack.
Numbers come only from a cloud instance (a non-burstable ~4-vCPU machine in Dograh's
production region, started only for the run). The laptop is for development; it
fails calibration under load.

Every step below that spends money stops after printing its projection. Nothing is
spent until a human approves and the command is re-run with `--yes`.

## 0. Before the run (human inputs)

These are decisions, not commands. The run does not start without them.

1. Spend approval for the projected Core cost (spec §9.3 estimates $300–700 per system).
   Write the approved cap into the run config as `max_usd`. Changing `max_usd` later
   is a new approval (CLAUDE.md §6 item 1).
2. The `private-test` pack and held-out pattern extensions, mounted at `DUPLEX_PRIVATE_DIR`.
3. Validated classifiers (M7b): kappa per classifier in the run card. A classifier below
   `kappa_min` stays marked `classifier` and out of headline rows.
4. The run config `configs/runs/core.yaml` with:
   - `systems`: Dograh stack + one reference S2S
   - `comparisons`: at most `holm_max_comparisons` pre-registered comparisons, each
     with `delta` and `discordant` so the planner can state power
   - `coverage`: `{metric_key: n}` the pre-registered minimum n per headline metric
     (Core: ≥ 100 valid events for each pooled Track A metric, spec §9.3)
   - `planned_calls` (optional): the number of attempts the plan makes

`gate` fails `power_notes` without `delta`/`discordant` and fails `coverage_n_met`
without a `coverage` block. These must be written before the run, never after.

## 1. Instance

```
uv sync
export DUPLEX_PRIVATE_DIR=/mnt/duplex-bench-private
uv run duplexbench doctor
uv run pytest -q
```

`doctor` must show `DUPLEX_PRIVATE_DIR ... (exists: True)` and the thresholds hash you
pre-registered. Write down the instance type (for example `c6i.xlarge`).

## 2. Calibration (on the instance, $0)

```
uv run duplexbench calibrate
```

Writes `results/calibration-<ts>.json`. It must pass. A failed calibration means the
machine cannot measure; do not run.

## 3. Pre-registration ($0)

```
uv run duplexbench preregister
uv run duplexbench planner --delta 0.2 --discordant 0.4 --pairs <planned pairs>
```

`preregister` hashes both system configs, the pack, the language pack, the thresholds,
the private tree and the harness commit. The planner prints the spec §9.2 table and
refuses "distinguishable" wording for an underpowered design. Keep its output.

## 4. Network baseline ($0)

```
uv run duplexbench netcheck api.openai.com:443 <dograh-host>:443 > results/netcheck-core.json
```

## 5. Projection, then the run (spend: approve first)

```
uv run duplexbench run configs/runs/core.yaml
```

Read the projection. Show it to the approver. Only after approval:

```
uv run duplexbench run configs/runs/core.yaml --yes
```

The run id is printed (`core-<UTC ts>`). The run stops at a budget refusal or at a
call whose actual cost is unknown; it never exceeds `max_usd`.

## 6. Run card facts ($0)

```
uv run duplexbench card-fill <run_id> --calibration results/calibration-<ts>.json \
    --netcheck results/netcheck-core.json --instance-type c6i.xlarge
```

## 7. Score, report, verify, gate ($0)

```
uv run duplexbench score <run_id>
uv run duplexbench report <run_id>
uv run duplexbench verify <run_id> results/<run_id>/report
uv run duplexbench gate <run_id>
```

`verify` must print `verify: 0 difference(s)`. `gate` prints one line per check:

| Gate | Passes when |
|---|---|
| `run_card_complete` | every spec §10 item 1 field is recorded (`missing` is empty) |
| `headline_n_ci` | every headline row has n > 0 and a CI |
| `power_notes` | a planner note exists for every headline row; every comparison declares `delta` and `discordant` |
| `calibration_passed` | the calibration recorded for the run passed |
| `harness_invalid_share` | harness-invalid calls / all calls ≤ `harness_invalid_max` |
| `attempts_preserved` | every scored call has a `calls` row with an outcome; `planned_calls` ≤ stored calls |
| `exclusion_rates` | `ambiguous_end` / `not_injected` / `unmatched_control` / `contested` rates are reported per system |
| `coverage_n_met` | every pre-registered `coverage` n is met by every system |

Present the report and the gate output. Wait for the decision on Full.

## 8. Phone path and load (M9; spend: approve trunk and run cost)

The trunk vendor is Open Question 5 (Twilio or Telnyx). Until a human chooses, the
real trunk (`TwilioTrunk`) raises `NotImplementedError("Q5: trunk vendor not chosen")`;
only the in-process `LoopbackTrunk` works.

Loopback baseline, before every phone-path run (`transport/loopback_baseline.py`,
`run_loopback`), stored with `card-fill --loopback <file>`. Phone-path latencies are
shown raw and with the loopback round trip subtracted, both labeled. Agreement with
the live stream is per-metric sign agreement of the paired differences
(`transport/phone_path.sign_agreement`); disagreeing metrics go to the run card as
`phone_path.disagreements` and appear as a caveat.

Concurrency pass (spec §4.4: c = 1 and c = 8, Track A, ≥ 100 events per level):

```
uv run duplexbench load configs/runs/<track-a run>.yaml --concurrency 8 --calls-per-level 100
uv run duplexbench load configs/runs/<track-a run>.yaml --concurrency 8 --calls-per-level 100 --yes
```

The first prints the projection for every level and stops (a priced system needs
`--yes`). A single `--concurrency N` runs levels (1, N); repeat the flag for more
levels. Output: `results/<run_id>/load_<run_id>.json` (per level: n, error / 429 /
disconnect rates with Wilson CIs, latency shift against c = 1, pacing slip and loop-lag
p99) and `results/<run_id>/sysmon_<run_id>.json` (CPU, memory, sockets, loop lag per second).

## Failure classes

A failure is either the harness's or the system's. Only the system's count against it.

| Class | Where it shows | Whose | What to do |
|---|---|---|---|
| `transport_error` | `calls.outcome` | harness (counted in `harness_invalid_share`) | read the call's `error` event; the attempt is kept, never deleted |
| pacing fail | `calls.flags_json.pacing_slip_p99_ms` > `pacing_slip_p99_ms` | harness | the machine is overloaded; rerun on a quieter instance |
| `load_invalid` | `calls.flags_json.load_invalid`, load report per level | harness | slip or loop-lag p99 over the limit at that concurrency; that level's numbers are not usable |
| calibration fail | `results/calibration-<ts>.json` | harness | do not run on this machine |
| `budget_refused` | `calls.outcome` | none (spend cap) | the cap was reached; a higher cap is a new approval |
| `timeout`, `stalled` | `calls.outcome` | system | scored as the system's outcome (spec §4.2) |
| `rate_limited` (429), disconnect | load report | system under load | reported per level, never retried silently |
| `ambiguous_end`, `not_injected`, `unmatched_control`, `contested` | observation tags | neither | expected outcomes; reported per system, excluded from the metrics they invalidate |
| `slow_caller`, `unscripted_overlap` | observation tags | simulator | excluded from latency / Track A metrics; counts in the caveats |

Never delete results, rerun a single call to replace a bad one, or change a threshold
to make a gate pass. Each of those is a human decision (CLAUDE.md §6).
