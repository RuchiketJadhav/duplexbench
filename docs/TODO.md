# TODO before the first run with defensible numbers

Opened 2026-11-06. Updated 2026-11-06 after the user's decisions on the 22-item list.
Owner "human" = a Dograh person. Owner "harness" = code work in this repo.
Decision records live in docs/decisions.md. Item numbers refer to the 22-item list of 2026-11-06.

## 1. Human inputs (the harness cannot produce these)

- [ ] **Private pack** (human, item 7). Write 40 `private-test` scenarios in the private repo.
      Mount that repo at `DUPLEX_PRIVATE_DIR`. Create each file with
      `uv run duplexbench scenario new --domain <d> --track <A|C|D> --split private-test`.
      Check each file with `uv run duplexbench scenario validate <dir>`.
      Also write 20 `public-dev` scenarios in `scenarios/public-dev/`. Five drafts exist since
      2026-11-06 (`pd_a_001`, `pd_a_002`, `pd_c_001`, `pd_c_002`, `pd_d_001`), checked with
      `scenario validate`. User decision 2026-11-06: the wording stands for the test run.
      **Remind the user to review the wording before the production (Core) run.** Wording open for the user's review. Placeholder voice names must map to Fish voice ids.
- [ ] **`.env` file** (human). Path: `C:\Users\Lenovo\research\duplex-bench\.env`. Copy
      `.env.example` and fill `OPENAI_API_KEY`, `DEEPGRAM_API_KEY`, `FISH_API_KEY`, `DOGRAH_API_KEY`.
      The file is git-ignored. `uv run duplexbench doctor` shows which keys are set.
- [ ] **Instance** (human, item 17). One non-burstable machine with 4 vCPU and 8 GB
      (c6i.xlarge or equal) in Dograh's production region. Start it only for runs.
      Calibration must pass on it.
- [ ] **Mount the private repo** on the instance (human, item 18). Export `DUPLEX_PRIVATE_DIR`.
- [x] **First commit** (item 19). Done 2026-11-06, commit `f609644`. The git email of this
      checkout is a placeholder (`your-github-email@example.com`). Set it before the first push.
- [x] **Reference ASR pick** (item 1). User decision 2026-11-06: Deepgram Nova-3 streaming,
      against the B10 reasons. Record and disclosures: docs/decisions.md B11. No model download,
      so Track C can run from the laptop. Needs `DEEPGRAM_API_KEY` in `.env`. Harness task
      refasr-deepgram builds the wrapper. After the first run, copy the reported `model_uuid` into
      `simulator.refasr.kwargs.pinned_model_uuid` and pre-register again.
- [ ] **Floor set** (human writes the list, harness renders and scores, item 9).
      What: 200 or more read-back items with a known written form and a known spoken form.
      Why: the reference ASR mishears digits on an 8 kHz line. The floor is its error rate
      with no agent on the call. An agent error at or below the floor is not a finding.
      How: write one YAML file in the private repo at `floor/floor_items.yaml`.
      One item per entry: `{type, written, spoken}`. Types: phone, email, zip, dob, amount,
      name, confirmation_code. Write at least 25 items per type. Use synthetic values only
      (555 numbers, example.com addresses). The 20-item example is `MINI_SET` in
      `src/duplexbench/refasr/floor_calibration.py`. The harness renders `spoken` with the
      stimulus TTS and plays it through C0 and C1. Blocked by harness fix-25, the pinned
      reference ASR and the instance.
- [ ] **Labeling sets** (human, two people, item 10). Explanation for the user:
      1. A program, not a person, decides three metrics. `respond_resume`: after a
         backchannel like "mm-hm", did the agent answer it or continue? `success_claim`: did
         the agent say "I booked it"? `recovery_request`: did the agent ask the caller to repeat?
      2. The program can be wrong. The benchmark proves that the program agrees with people
         before it reports the number as a headline. That proof is the labeling.
      3. After the first real run, the harness cuts a short clip around each decision and
         writes a manifest file. One line per clip (JSONL: item_id, text, wav, t0_ms, t1_ms).
         Today no command writes that manifest. See harness fix-26.
      4. Person A runs `uv run duplexbench label --manifest <file> --set respond_resume --labeler <id>`.
         The tool prints the clip path and the reference-ASR text. It asks one question.
         Person A types one answer per clip (respond, resume or uncertain). The tool saves
         each answer at once, so a session can stop and resume.
      5. Person B runs the same command on the same file.
      6. Three sets, 200 clips each: `respond_resume`, `success_claim`, `recovery_request`.
         About two hours per person per set.
      7. The harness computes agreement (kappa). At 0.7 or above, the metric enters the headline
         rows. Below 0.7, the metric stays tagged `classifier` and off the headline rows.
         The run stays valid in both cases.
- [ ] **Workflow** (human builds through MCP, item 6). User 2026-11-06: the harness side writes
      the exact specification, the user builds it. Specification: docs/WORKFLOW_SPEC.md
      (written 2026-11-06: one "DuplexBench Support Line" workflow, router in the start node,
      five domain nodes, verbatim prompts, 20 tool bodies, settings, acceptance checks, MCP
      build order, gaps G1 to G10 in its section 9). Standards. The workflow must meet all of these:
      1. It is the production configuration or an exact copy (same STT, LLM, TTS, prompts, turn
         detection). The run card records its `definition_id` and a hash of the definition plus
         the org model configuration (`params.org_model_config`).
      2. English (US) only.
      3. Its domains match the pack. The harness mock backends serve five domains. Each Track C
         scenario names tools from one domain. The workflow must handle every domain the pack uses.
      4. Every tool a scenario names exists in Dograh as an org-level HTTP tool with the same
         name. Method POST. URL `http://<harness-host>:<port>/tools/{{call_id}}/<tool_name>`.
         Body = the LLM arguments as JSON. Keep the port literal. The names the harness serves:
         - scheduling: find_slots, book_appointment, reschedule_appointment, cancel_appointment
         - orders: lookup_order, cancel_order, update_shipping_address, create_return
         - billing: get_balance, make_payment, issue_refund
         - account: verify_identity, get_account, update_phone, update_email, update_address
         - outbound: get_contact, record_confirmation, schedule_callback, mark_do_not_call
      5. Each tool's `timeout_ms` is larger than the largest injected tool latency in the pack.
         Record the value.
      6. No `customMessage` on any tool. If one exists, record it. It changes `t_tool_to_info_ear`.
      7. Template variables set: `provider: twilio` and `telephony_configuration_id`.
         Published, with no draft left.
      8. Who speaks first is known and matches `simulator.caller_first` in the run config.
      9. The workflow verifies identity before an account change. Without that rule,
         `auth_bypass_rate` and `policy_violation_rate` measure nothing.
      10. No live side effect. Tools point only at the harness. No real SMS, email or payment.
      Also pick one reference speech-to-speech system to run beside it. Recommendation:
      `gpt-realtime` (the only adapter built from documentation the team checked against the provider). Decide after item 12.
- [ ] **Dograh-side setup** (human with harness help, item 14). Blocked by the workflow choice
      and the instance. Steps, from docs/dograh-ingress.md:
      1. `POST /api/v1/organizations/telephony-configs` with provider twilio and placeholder
         `account_sid` and `auth_token`. Keep the returned `config_id`.
      2. `PUT /api/v1/workflow/{wf}` with the template variables of standard 7. Then publish.
      3. Create one HTTP tool per tool name (standard 4). Attach them to the workflow nodes.
      4. Create an API key. Export it as `DOGRAH_API_KEY`. Set `workflow_id`, `org_id`,
         `endpoint` and `harness_tool_base` in `configs/systems/dograh_stack.yaml`.
      5. Open the Dograh box's network to the harness host and port (security group).
      6. Walk the live checklist in docs/adapters.md during the five-call smoke.
      The harness needs a running Dograh with the production STT, LLM and TTS configuration,
      not the Dograh source. It creates runs over REST, streams audio over the telephony
      WebSocket, and Dograh calls the harness tool server over HTTP. The user ruled out Dograh
      in Docker on the laptop (memory). Two hosts remain:
      1. The EC2 box (`dograh-box`), a full replica. Start it for the run, stop it after.
         The harness can run on the laptop for the test run with one open port for tool calls.
      2. Dograh's hosted SaaS. Works only if (a) the harness tool server has a public address
         (SaaS mode rejects localhost and private IPs, `api/utils/url_security.py`), and (b) the
         telephony WebSocket token is not enforced there, or the team shares
         `TELEPHONY_WS_TOKEN_SECRET` so the harness can mint the token. Both are questions for
         the Dograh team. The Dograh MCP server manages workflows only. It carries no audio.
      Recommendation: the EC2 box for the test run. Status 2026-11-06: ssh to the box timed out.
- [ ] **Private pattern and token extensions** (human, item 8).
      What: five YAML files with the same names as `configs/langpacks/en-US/`:
      `ycmd_tokens.yaml`, `hbc_tokens.yaml`, `success_patterns.yaml`,
      `recovery_patterns.yaml`, `normalizer_rules.yaml`.
      Where: `$DUPLEX_PRIVATE_DIR/langpacks/en-US/`. A file may hold any subset of the keys.
      Lists append to the public lists. Pattern ids must be unique across public and private.
      How: copy the public file, delete the public entries, add 5 to 10 held-out phrasings per
      list that a vendor cannot find in this repo. Example wait phrase: "hang on a sec".
      Example success phrase: "all set, that's booked". The language-pack hash changes, so run
      `preregister` again. Check: `uv run duplexbench doctor` prints the private dir.
      Optional for the test run. Required before publication.

## 2. Decisions

- [x] **Gilbert-Elliott loss form** (item 2). Keep the plan default `ge_loss_in_bad: 1.0`.
      User decision 2026-11-06. Record: docs/decisions.md B7.
- [x] **Simulator LLM** (item 3). PhoneLLM Alpha 1 (`pipecat-ai/phonellm-alpha-1`) through an
      OpenAI-compatible endpoint. User decision 2026-11-06. Record and condition: B8.
      Open for Core: who hosts the endpoint (vLLM on a GPU box, or a Modal endpoint). The model
      needs one large GPU (the model card tests one B200). Set `simulator.llm.base_url` when known.
- [x] **Test-run simulator LLM** (user 2026-11-06: "openai's model for test"). OpenAI
      `gpt-5.4-mini-2026-03-17` through `simulator.llm` with `reasoning_effort: none`, no
      temperature sent. `gpt-4.1` is deprecated. Test run only, since OpenAI has systems under
      test. Record: B12. Needs `OPENAI_API_KEY` in `.env` (today `.env` does not exist).
- [x] **Test-run spend cap** (item 4). `max_usd: 20` for the test run. The Core cap comes after
      the test run. Record: B9.
- [ ] **Coverage and comparisons** (item 5). The user is not sure. Context, recommendation and
      reasoning:
      - Context. `coverage: {metric_key: n}` is the smallest number of valid measurements per
        headline metric that the run must reach. `comparisons` is the list of system pairs the
        report may call different. Each has `metric`, `a`, `b`, `delta` and `discordant`.
        At most six (`holm_max_comparisons`). Both blocks go into the run config before the run,
        so nobody can stop early or pick pairs after the result. The gate fails `coverage_n_met`
        and `power_notes` without them.
      - Recommendation for the test run: write neither block. The gate reports two failed checks.
        The test run is not for claims.
      - Recommendation for Core [Proposed]. Coverage: 100 valid events for each of `yield_rate`,
        `hold_speaking_rate`, `hold_listening_rate` and `response_rate` (spec §9.3). 120 valid
        calls for `pass_at_1` (40 scenarios times 3 trials). 200 valid items for
        `readback_error_rate`. Comparisons, all `a: dograh_stack`, `b: <reference S2S>`,
        condition C0: `t_turn_gap_ear`, `t_stop_ear`, `yield_rate`, `pass_at_1`,
        `readback_error_rate`. That is five. Keep the sixth slot empty.
      - Reasoning. `delta` is the smallest difference a buyer would act on. For a rate it is a
        proportion, for a latency it is in milliseconds. `discordant` is the share of paired
        trials where the two systems differ. The planner turns both into the number of pairs the
        run needs: `uv run duplexbench planner --delta <d> --discordant <q> --pairs <n>`.
        Run it before you fix the values. Start from `delta 0.10, discordant 0.30` for rates.
- [ ] **Models to test, then adapters** (item 12). User: decide the model list first, then write
      the adapters. Adapters that exist: openai_realtime, gpt_live, gemini_live, grok_voice,
      pipecat_cascade, selfhosted_fd (Moshi), dograh_stack. A new model needs a new adapter and a
      five-call live smoke. Thirteen event mappings across the non-OpenAI adapters wait for their smoke
      before the harness trusts them.
- [ ] **Dograh workflow handoff** (2026-11-08, pasted from the workflow session, verified live).
      Hosted app https://app.dograh.com/api/v1, org 6993, workflow 14735 "DuplexBench Support Line",
      published version 3 (definition 42706, no draft), telephony configuration 5767 (placeholder
      Twilio), template vars provider twilio. Realtime override under the v3 key
      `model_configuration_override` (B23), gpt-realtime-2.1, voice alloy. Agent speaks first:
      the run config needs `scripted.lead_in: agent_first`. 21 tools point at HARNESS_HOST (placeholder).
      Open on the harness side: (1) tunnel hostname to the workflow session, then they update the
      21 tool URLs and run acceptance checks 4 and 5. If the tunnel has no port 8900, the URL pattern
      changes. (2) WS token enforcement on hosted unknown, off by default in the source. (3) Every
      run is a paid OpenAI realtime call. Approval before checks 7 to 9 and any smoke. (4) Any republish
      must leave no draft, POST /runs uses a draft when one exists. Harness work: fix-43 (adapter
      reads the v3 key, resolves the provider via /model-connections/model-configurations), system
      YAML ids, `configs/runs/test_dograh.yaml`. fix-43 and the YAML ids built 2026-11-08 (972 tests,
      config_id 05c2ba5818d9825e: re-run `preregister` before any run). Spec updated for the hosted
      build (§1.1 as built, §5.4.1 v3 shape, G13 to G16). Still unseen live: the model-connections
      response shape, `GET /api/v1/tools/` on hosted, the provider connection itself is outside the
      hash (only `model_names` on the run card shows a swap). Spec note: the override sets
      `language: en` for OpenAI where §5.4 said to leave it unset. Harmless for English.
- [ ] **Flaky timing test under load.** `test_refasr_deepgram_provisional::test_partial_p90_with_interims_under_the_gate`
      reported a 64 s partial p90 once while three suites ran at the same time, and passes alone.
      Real-clock test. Mark it `timing` or bound it with the fake server's own clock.
- [ ] **Dograh model override** (decision B21). The workflow must run a full-duplex model, not
      the org's cascade. Pick the first model for the test run (recommendation: OpenAI
      `gpt-realtime`, the harness already has its adapter and price file), set the override and
      the provider key in Dograh, and name the model in `configs/systems/dograh_stack.yaml`. One
      Dograh system per model. Spec updated 2026-11-07 (§5.4: `model_configuration_v2_override`,
      mode byok, `byok.mode: realtime`, provider key inside the override, read back masked). Use
      `gpt-realtime-2.1` (`gpt-realtime` retires 2027-01-20). On the realtime path Dograh mutes the
      caller until the greeting completes, so fix-34 `lead_in: agent_first` is required for Track A.
      Gaps G11 (hosted app accepts the override?) and G12 (Gemini and Grok model names) are human checks.
- [x] **Reference-ASR latency gate, resolved by inspection of Dograh** (2026-11-08). Dograh does not
      have this problem because pipecat sends Deepgram `Finalize` the moment its local VAD sees
      the speaker stop (`pipecat/services/deepgram/stt.py` ~804-810), which flushes the final at
      once. Pipecat's stt-benchmark (reference/stt-benchmark/README.md) defines the latency that
      matters as TTFS, speaker stop to final transcript, and measured Deepgram nova-3-general at
      p50 247 ms, p95 298 ms, p99 326 ms that way. The harness gate measures TTFS with
      Finalize-on-stop instead of per-word partial latency. Dograh also uses endpointing 100,
      never 10. Built 2026-11-08 as fix-40 (926 tests): `DeepgramStreamer.finalize()`, the
      simulated caller sends it on every agent stop, `calibrate` measures TTFS per utterance with
      the playout segmenter's rule, endpointing default 100. The calibration clip's sentence pauses
      were widened to 800 ms so it holds 13 utterances. **Decided B22 (user, 2026-11-08):** gate =
      TTFS p90 <= 700 ms = `sim_eot_ms`, hold included. The laptop fails it (about 300 ms is India
      to US RTT), EC2 should pass. No override flag. The first smoke run happens on EC2 after
      everything else is done.
      Live 2026-11-08 (results/calibration-20261108T034648Z.json): TTFS p50 801 / p90 3893 ms, wait after
      the stop p50 301 / p90 401 ms, 2 of 13 utterances at 4.6 and 5.6 s (words arrived only with the
      next chunk final). Diagnostic rerun: all 13 in 461 to 880 ms, of which about 300 ms is network
      RTT from this laptop. Deepgram's own endpointing-100 final beats the Finalize reply at a 500 ms hold.
- [x] **fix-42 speculation from provisional words** (built 2026-11-08, 11 tests. Per-row tags are not persisted, see worklog). Spec §7 says generation starts speculatively
      from partial ASR at `sim_speculate_ms`. The code keys speculation on the finals-only
      transcript (fix-24 kept provisional words out of it). Let the speculative draft start from
      settled provisional words and restart when the finals differ. The scored transcript stays
      finals-only. Then a late Deepgram final costs one discarded draft, not a stalled caller.
- [x] **Longer calibration clip** (clip made 2026-11-08: tests/fixtures/refasr_calib_speech_3min, 42 utterances, 0 USD. Default since fix-41. A live calibration now takes 3.5 min and 0.025 USD). 60 s gives 13 utterances, so one Deepgram outlier (empty
      Finalize reply, words with the next chunk final, seen 2 of 13 then 0 of 13) can break the
      p90. Make a 3 min clip (about 40 utterances, 0.023 USD per calibration) in the same
      read-back style. If the outlier rate is really near 8 percent the gate still fails, which
      is the right outcome.
- [x] **fix-41 calibrate diagnostics** (built 2026-11-08: `<calibration>.refasr_log.json` on any fail, `--keep-refasr-log`, finish timeout names the last message). On a refasr gate fail, write the Deepgram message log
      (`DeepgramStreamer.messages` with arrival times) next to the calibration JSON, so an outlier can
      be explained without a second paid run. Also log `finish()` timeouts with the last message seen.
- [ ] **Core `max_usd`**. Set after the test run.
- [x] **Workflow spec gaps** (docs/WORKFLOW_SPEC.md §9), user decisions 2026-11-06 (B15 to B18):
      G2 add `verify_identity` to billing with a per-scenario gate. G4 seed appointments. G5
      `get_balance` returns the cap. `max_call_duration` 600, set in the workflow build. Backends
      done 2026-11-06 (847 tests): billing `verify_identity` (gate on with
      `policy.require_identity: true`), seed appointments AP-101 and AP-102, `refund_cap` in
      `get_balance`, and a per-scenario `seed:` block that overrides SEED rows, wired into
      `cli_run`. The spec follows (21 tools, billing `verify_identity` is a second org-level row
      with the same name, so the builder must keep the two tool uuids apart).
- [x] **G10, policy scoring**. Built 2026-11-07 (863 tests, decision B20): violation rows tagged
      `code:<c>` and `refusal_reported:true|false`, `pass_at_1` survives an honestly reported
      refusal. Open: the disclosure patterns (d01 to d18) need human labels before they count
      as checked against real speech. Until fix-23 feeds reference-ASR words to `cli score`, every refusal reads
      as unreported, so scored runs behave as before.
- [ ] **Emotion in caller audio (proposal, no change made)**. The user picked `drama-3` so the
      caller can sound angry, rushed, tearful or hesitant and stress the agent. Drama 3 takes a
      direction in square brackets before the words (`[angry, talking fast] I already told you`)
      and vocal events inline (`[sigh]`, `[shaky breath]`). Proposed design, for a decision:
      1. Add an optional `delivery: str` field on `CallerStep` and `Overlay`. The TTS client
      prepends it as `[delivery]`. The scored `text` stays clean, so word matching and the
      pack hash of the words do not change.
      2. Treat delivery as a scenario condition like noise or codec: `emotion: neutral|stressed`
      twins of the same scenario, so the metric delta is attributable to delivery alone.
      3. Private pack only at first. Public-dev stays neutral so the public baseline is simple.
      4. Record `delivery` in the stimulus `.json` and in the run card.
      Risks to check at the smoke: whether the word timestamps skip the bracketed direction
      (overlay timing depends on them), whether vocal events shift the speech onset the
      importer detects, and the price: drama-3 bills the direction bytes too.

## 3. Harness work

- [x] fix-20: simulated caller hears reference-ASR text. Provider transcript kept for `mishearing_rate`.
- [x] fix-21: normalizer comma boundary for Track D entities (`en-entity-3`).
- [x] fix-22: Dograh adapter plan-time preflight (wired into `check_plan`). Fresh pre-registration record.
- [x] sim-llm: OpenAI-compatible client for PhoneLLM and the `simulator.llm` run-config block.
      Done 2026-11-06 (755 tests). Gap: the run card's `simulator` field is still None, so the
      `billing: external` disclosure sits only in the call flags. See fix-27.
- [x] fix-23: `cli score` runs the run's reference ASR on each call's ear channel (found in the
      WAV by matching frame RMS against the playout rows) and feeds the words to the Track C and
      Track D scorers. Done 2026-11-07 (893 tests). Words cached in `<call_id>.refasr.json`, so a
      rerun streams nothing. Paid engines print a projection and need `--yes`. Without a configured
      reference ASR the word-based observations are invalid, tagged `no_refasr`. Open: the new
      tags `no_refasr` and `step_not_reached` are not in `metrics/base.TAGS`. The run card does not
      show the score block (golden M7 would change). Scoring sends audio faster than real time,
      and Deepgram's behaviour on that is unchecked. `mishearing_rate` still uses the live text.
- [ ] fix-25: `cli floor`. Read `floor/floor_items.yaml` from the private repo. Render `spoken`
      with the stimulus TTS. Run `floors()` on 200 or more items under C0 and C1. Add a Wilson
      interval. Write `results/floor-<ts>.json`. Feed the run card. Today only the 20-item
      `floor_mini` exists.
- [x] fix-27: run card `simulator` block filled (`scripted` {lead_in, tts from clip sidecars} and
      `simulated` {caller_first, llm, refasr final id, tts}). Done 2026-11-07 (914 tests). Tags
      `no_refasr` and `step_not_reached` are in TAGS, excluded from rates and listed in the caveats.
      Open: `below_floor` and `no_floor` are still not handled by the report. The HTML exclusions
      table has a fixed column list.
- [x] refasr-deepgram: Deepgram Nova-3 streaming wrapper (`refasr/candidates/deepgram.py`),
      candidate `deepgram`, model_uuid recorded per stream and in the run card. Done 2026-11-06
      (778 tests). Price in `configs/prices/deepgram_nova3.yaml`. One stream per call, every ear
      frame billed. First paid stream 2026-11-07 (0.0077 USD): model uuid
      `40bd3654-e622-47c4-a111-63a61b23bfe8` (decision B19, pin it). The 300 ms partial-latency
      gate is still unmeasured because the calibration clip is not speech (fix-39).
- [x] fix-28: Track C projection, budget and stored actual cost include the reference-ASR
      minutes and the caller-LLM tokens (`simulator.llm.price_ref: openai_chat`). Done 2026-11-06.
      Inputs are [Assumption], decision B14. Gap: `cli load` does not call
      `apply_simulator_costs`, so a load run over Track C projects too low.
- [x] fix-29: `calibrate --refasr-config <run yaml> [--yes]` runs the reference-ASR stage with
      the engine named by `simulator.refasr`. Paid engines print the cost and need `--yes`.
      Done 2026-11-06 inside refasr-deepgram.
- [x] fix-30: pre-registration hashes the run config's `simulator` and `scripted` blocks
      (`hashes.run_config`, secrets dropped, prompt stored as sha256). Done 2026-11-07 (881 tests).
      Register with `duplexbench preregister --run-config <run yaml>`. Every older record is stale
      by design and refuses with "predates run-config hashing: re-register".
- [x] tts-fish: Fish Audio client (`stimuli/fish_tts.py`, word timestamps from the
      `with-timestamp` route, decision B13). Done 2026-11-06 (816 tests). `stimuli generate
      --vendor fish --model drama-3 --voices-file configs/voices/fish.yaml --default-voice delia`.
      Projection uses the $50 list price, so it reads high until 2026-11-30.
      `simulator.tts` is wired into `cli_run` (2026-11-06). Open: user decision 2026-11-06: one default voice (`delia`) for every scenario voice name, no voice
      map. Check at the paid smoke
      that `drama-3` serves the with-timestamp route and record one real stream for the tests.
- [x] fix-31: `stimuli generate` writes `clip`, `speech_onset_ms` and `speech_end_ms` back into
      the scenario YAML and synthesizes `<stem>_resumption.wav` for Y-CMD overlays and paused
      steps (`Overlay.resumption_text` added, `--no-write-back` keeps the old behaviour). Done
      2026-11-06 (828 tests). The five public-dev drafts now run with fake audio (5.5 MB under
      `scenarios/public-dev/audio/tts/`, commit the drafts and the audio together or neither).
      The pack hash changes after every generate, so pre-register afterwards. Open: `pack_items`
      still synthesizes step text for simulated Track C scenarios that nothing plays (10 of 35
      items, 574 of 1,616 characters at the real vendor).
- [x] fix-32: `required_spoken` is Track C only (asserted). `entities[].step` counts only agent words
      after that caller step ended (`step_not_reached` when it never did). Done 2026-11-07.
- [x] fix-33: `scenario validate`, `cli run` and `preregister` check tool names, kinds, `after_tool`
      and expectation args against the domain's mock backend. Done 2026-11-07. The test fixture
      `tests/fixtures/scenarios/public-dev/track_c_001.yaml` uses tool names no backend has, so the
      tests load it with `check_tools=False`. Rename its tools later.
- [x] fix-34 (spec gap G1): `scripted: {lead_in: agent_first}` makes the scripted caller wait for
      the agent's first end of turn (ear silent for `eend_silence_ms` after the last stop) before
      step 1. Default `caller_first` keeps today's behaviour. No greeting within `stall_ms` sets the
      `no_greeting` flag and starts step 1. Done 2026-11-07. Flags: `greeting_end_ms`,
      `step1_start_ms`, `greeting_utterances`. Open: the 6 s end-of-turn wait counts an early
      re-prompt as greeting (a shorter wait would be a threshold decision). `est_call_minutes` does
      not count the lead-in. The report does not sum greeting times across calls.
- [x] fix-35 (spec gap G8, built 2026-11-08. The `GET /api/v1/tools/` rows the workflow references are hashed. Hosted-app route unverified): hash the 20 Dograh tool definitions (`GET /api/v1/tools/`) into the
      Dograh system config, since a tool edit changes behaviour without changing `config_id`.
- [x] fix-36 (spec gap G9, built 2026-11-08: `model_source` workflow_override | org_config | pasted | unknown): read the org model configuration from
      `GET /api/v1/user/configurations/user` in the Dograh adapter and hash it, instead of the
      pasted `params.org_model_config`.
- [x] fix-37 (spec gap G6, built 2026-11-08, two mute disclosures in the adapter): Dograh mutes the caller during every tool call, so a
      correction spoken while a tool is pending is not heard. The run card must say so.
- [x] fix-38: the CLI never loaded `.env` although every message said "set X in .env".
      `load_dotenv(override=False)` now runs in the Typer callback. Done 2026-11-07.
- [x] fix-39: the calibration clip is now real speech, `tests/fixtures/refasr_calib_speech_60s.wav`
      (Fish `s2.1-pro-free`, 0 USD, 54.6 s of speech, 181 words, transcript in the `.txt` beside
      it, Fish word times in the `.json`). Done 2026-11-07. Deepgram returned all 181 words. Open:
      the stage does not yet compare the words with the transcript (word accuracy).
- [x] fix-24: Deepgram releases interim words as provisional (`Word.final`, `stream(provisional=True)`),
      finals exactly once. Transcripts use final words only. Done 2026-11-07 (906 tests).
      Measured on the real API, 54.6 s of speech, 4 streams (0.03 USD):
      finals-only partial p90 3990 ms. With interims p90 1160 ms. Deepgram sends one interim about
      every 960 ms (p90 1040 ms), so the 300 ms gate cannot be met by Deepgram at all.
      End-of-utterance final latency at 13 pauses: p50 572 ms, p90 1972 ms, max 3401 ms.
      Logs: scratchpad `dg_eou_log.json`. Decision needed (thresholds are human-owned), see §2.
      Open: the caller LLM's transcript grows at finals only. `endpointing=10` gave no early finals.
- [ ] fix-26: `label-manifest` command. After a run, cut one clip per classifier decision and
      write the JSONL manifest that `label` reads. Today no command writes it.
- [ ] **Simulator smoke** (item 13, user: yes). A few Track C calls on `public-dev` scenarios.
      Needs: `OPENAI_API_KEY` and `DEEPGRAM_API_KEY` in `.env`, a calibration whose
      reference-ASR stage passed, three to five `public-dev` Track C scenarios, and a running
      Dograh (or the OpenAI Realtime adapter as the agent for a first plumbing check).
      Cost: the agent's call minutes, Deepgram at $0.0077 per audio minute and the caller
      LLM tokens, under the 20 USD cap.
      Command: `uv run duplexbench run configs/runs/test_dograh.yaml --yes`.
- [x] **Test-run config** `configs/runs/test_dograh.yaml` with `max_usd: 20`. Written 2026-11-08 after
      the user saw it. Dry run: 2 Track C scenarios in public-dev (pd_c_001, pd_c_002), so 2 calls,
      projected 0.20 USD (agent 0.169 at gpt-realtime-2.1 prices through `price_ref: openai_realtime`
      on dograh_stack, ref ASR 0.014, caller LLM 0.020). Refused until `preregister --run-config`
      runs again, which waits for the tunnel hostname in the system YAML. Original item: Written when the
      workflow id and org id are known. The file contents are shown in chat before it is written.
