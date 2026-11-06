# Dograh Duplex Bench

A measurement harness for full-duplex voice agents on the phone channel. It plays a
caller into a system under test (Dograh's stack, hosted speech-to-speech models, a
cascade baseline, self-hosted full-duplex models), records what the caller would hear,
and scores turn-taking (Track A), task success under conversation (Track C), entity
capture (Track D) and latency, load and cost (Track L). Every number comes with its n
and a confidence interval. There is no composite score.

The spec is [docs/SPEC.md](docs/SPEC.md). The build order is [CLAUDE.md](CLAUDE.md) and
[docs/BUILD_PLAN.md](docs/BUILD_PLAN.md). Progress is in [docs/worklog.md](docs/worklog.md).

## Install

Python 3.12 and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run duplexbench doctor
uv run pytest -q
```

Secrets go in `.env` only (copy `.env.example`). Nothing else reads credentials.

## Private files: `DUPLEX_PRIVATE_DIR`

The `private-test` scenarios, their audio, the floor-set audio and the held-out
pattern extensions live in a separate private repository. Mount it and point the
harness at it:

```
export DUPLEX_PRIVATE_DIR=/path/to/duplex-bench-private
```

This repository never contains a private file (`tests/unit/test_no_private_files.py`
enforces it). Pre-registration stores only the private tree's hash.

## Money: nothing is spent without `--yes`

Every command that can call a paid API (provider calls, simulator, stimulus TTS,
trunk minutes, GPU hours) prints its projected cost first and stops. It runs only
when you pass `--yes`, and never beyond the run config's `max_usd`. A $0 run (the
mock agent) needs no approval.

## The pipeline

Each step reads what the previous one wrote under `results/<run_id>/`.

| Step | Command | What it does |
|---|---|---|
| 1 | `uv run duplexbench doctor` | Python, clock, `DUPLEX_PRIVATE_DIR`, thresholds hash |
| 2 | `uv run duplexbench calibrate` | Mock-agent calibration on this machine (spec §1.4); writes `results/calibration-<ts>.json` |
| 3 | `uv run duplexbench preregister` | Hashes systems, pack, langpack, thresholds, private tree, commit; writes `results/preregister-<ts>.json` |
| 4 | `uv run duplexbench run configs/runs/<run>.yaml [--yes]` | Prints the projection; runs the calls only with `--yes` |
| 5 | `uv run duplexbench score <run_id>` | Computes observations from the event log |
| 6 | `uv run duplexbench report <run_id> [--public]` | HTML report, `aggregate.json`, `run_card.json` |
| 7 | `uv run duplexbench verify <run_id> results/<run_id>/report` | Recomputes every number from the store; `0 difference(s)` is clean |
| 8 | `uv run duplexbench gate <run_id>` | The M8 release gate; one line per check, exit 1 on any failure |

Supporting commands: `planner` (spec §9.2 sample sizes), `netcheck` (network
baseline), `card-fill` (write netcheck / loopback / SysMon / calibration into the
run card), `load` (open-loop concurrency levels), `label` (two-labeler validation
sets), `scenario` (template and validator), `stimuli` (approve-first TTS).

A first $0 run against the mock agent (the full eight-step log is in
[docs/smoke_mock_log.md](docs/smoke_mock_log.md)):

```
uv run duplexbench preregister --pack-dir tests/fixtures/scenarios/smoke
uv run duplexbench run configs/runs/smoke_mock.yaml --yes
uv run duplexbench score <run_id>          # default store: results/<run_id>/events.sqlite
```

(`--allow-unregistered` skips the pre-registration check for public-dev packs.)
System configs put adapter-specific settings under `params:` (every file in
`configs/systems/` must load through `systems.load_system`; a test enforces it).

## Documentation

- [docs/runbook.md](docs/runbook.md): the Core run procedure, spend approvals, failure classes
- [docs/thresholds.md](docs/thresholds.md): every threshold, generated from the code
- [docs/SPEC.md](docs/SPEC.md): what is measured and why
- [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md): tasks, signatures and acceptance tests
- [docs/decisions.md](docs/decisions.md): decisions and build-time notes
- [docs/providers.md](docs/providers.md): confirmed provider facts (formats, events, prices)
- [docs/adapters.md](docs/adapters.md): adapter notes
- [docs/dograh-ingress.md](docs/dograh-ingress.md): how the harness reaches Dograh's stack
- [docs/reference-notes.md](docs/reference-notes.md), [docs/licenses.md](docs/licenses.md): reference repos and their licenses
- [docs/refasr-candidates.md](docs/refasr-candidates.md): reference-ASR candidates
- [src/duplexbench/README.md](src/duplexbench/README.md): code layout and clock rules
