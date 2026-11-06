# TODO — before the first run with defensible numbers

Opened 2026-11-06. One line per item. Owner "human" = a Dograh person; "harness" = code work.

## Human inputs (the harness cannot produce these)

- [ ] **Private pack** (human): 40 `private-test` scenarios in the private repo, mounted at
      `DUPLEX_PRIVATE_DIR`. Use `uv run duplexbench scenario new --domain <d> --track <A|C|D>
      --split private-test` for the template and `uv run duplexbench scenario validate <dir>`
      before every run. Also 20 `public-dev` scenarios in `scenarios/public-dev/`.
- [ ] **Instance** (human): a non-burstable 4 vCPU / 8 GB machine (c6i.xlarge or equal) in
      Dograh's production region, started only for runs. Calibration must pass on it.
- [ ] **Mount the private repo** on that instance and export `DUPLEX_PRIVATE_DIR`.
- [ ] **First commit** of this repo (pre-registration records the harness commit).

## Decisions still open (CLAUDE.md §6)

- [ ] Reference ASR: run `refasr-bench --yes` on the cloud instance (laptop cannot hold the
      models), pick one, pin it.
- [ ] Gilbert-Elliott loss form (recommended 2026-11-06 in chat: keep the plan default 1.0; record in docs/decisions.md once answered).
- [ ] Simulator LLM client and model (user intent: a Pipecat-served LLM through its API;
      family must have no voice system under test, spec §7).
- [ ] `max_usd` for the first paid run (set after the test run).
- [ ] Coverage n and comparisons (`delta`, `discordant`) in `configs/runs/core.yaml`.
- [ ] Which published Dograh workflow is "the Dograh stack"; which reference S2S beside it.

## Harness work (2026-11-06)

- [x] fix-20: simulated caller hears reference-ASR text; provider transcript kept for `mishearing_rate`.
- [x] fix-21: normalizer comma boundary for Track D entities (`en-entity-3`).
- [x] fix-22: Dograh adapter plan-time preflight (wired into `check_plan`); fresh pre-registration record.
- [ ] fix-23: `cli score` must run the pinned reference ASR on the ear channel and feed its words to
      `score_track_c` and `entities.score` (today it passes an empty list, so Pass@1 required_spoken,
      `t_tool_to_info_ear` and Track D cannot score on a real run). Needs the pinned model.
- [ ] fix-24: streaming partials for the pinned reference ASR (all candidate wrappers are batch-only,
      so the simulated caller hears an utterance only ~500 ms after it ends). Decide after the model pick.
- [ ] Simulator LLM client for the user's Pipecat-served endpoint, once the model family is confirmed.
