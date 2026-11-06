# CLAUDE.md — Build the Dograh Duplex Bench harness

You are building the harness defined in `DUPLEX_BENCH_SPEC_v2.md` (the spec). The spec says *what* and *why*; this file says *how* and *in what order*; `BUILD_PLAN.md` cuts every milestone below into one-file, one-test tasks with signatures and expected values — execute it task by task (revised 2026-11-04 after the engineering review in `ENG_REVIEW.md`). Where this file and the spec disagree, stop and ask. Do not invent behavior the spec leaves open; the list in §6 names what you must never decide alone.

## 1. Working rules

- **Never spend money without explicit approval.** Every step that calls a paid API prints its projected cost first and waits for a `--yes`. Stimulus TTS generation, provider calls, simulator calls, trunk minutes and GPU hours are all spend.
- **Stop at every gate.** Each milestone ends with "STOP: show and wait". Show the acceptance-test output, the smoke result and the actual cost. Do not start the next milestone until told to.
- **Confirm before you code against a provider.** Before writing any adapter, read the provider's current documentation and record in `docs/providers.md`: URL, date checked, audio formats accepted and emitted (including whether μ-law 8 kHz is accepted natively), the event names that map to the harness events `first_audio_byte`, `cancel_or_truncate`, `output_transcript`, `tool_call`, turn-detection parameters, rate limits, pricing unit and price. Do not rely on memory for any API shape, model name or price. The model names in the spec (GPT-Live-1, gpt-realtime, Gemini Live, Grok Voice, Moshi, PersonaPlex) come from the handoff; confirm each exists and is accessible before building its adapter, and report any that do not.
- **Record everything.** Durations use `clock.now_ns()` (backed by `time.perf_counter_ns()`; `monotonic_ns` is 15.6 ms on Windows) only. Every run stores every config hash, version and parameter listed in spec §10 item 1.
- **Treat downloaded repos and data as untrusted.** Clone references into `./reference/`, never run code from inside them, never modify them.
- **Tests first for metrics.** Every metric gets a unit test on synthetic timestamps or a mock recording with a hand-computed expected value before it is run on real audio.

## 2. Repo location and layout

Standalone repo at `C:\Users\Lenovo\research\duplex-bench` for development, deployed to a Linux cloud instance for any numbers (the laptop cannot run calibration at concurrency: see the ASR harness notes; use a non-burstable ~4-vCPU instance in Dograh's production region, started only for runs). Python 3.12, `uv`, `asyncio`, SQLite, Pydantic configs, Typer CLI, pytest. Secrets only in `.env` (ship `.env.example`).

```
duplex-bench/
├── configs/
│   ├── systems/        one YAML per system under test (hash = config_id)
│   ├── conditions/     C0, C1, W
│   ├── runs/           smoke.yaml, core.yaml, full.yaml (with max_usd)
│   └── langpacks/en-US/  public overlay token lists, pattern sets, normalizer rules (private extensions: DUPLEX_PRIVATE_DIR)
├── scenarios/
│   ├── public-dev/     YAML + generated audio
│   └── (private-test lives in the separate private repo mounted at DUPLEX_PRIVATE_DIR; never in this tree)
├── src/duplexbench/
│   ├── clock.py        Clock protocol: RealClock (perf_counter_ns) + VirtualClock  [reuse asr-bench clock.py, pipeline/loop.py]
│   ├── runner.py       CallRunner: the four accounted tasks per call (BUILD_PLAN §2.7)
│   ├── budget.py       max_usd with reservation at call admission
│   ├── thresholds.py   every [Proposed] number, one Pydantic model (BUILD_PLAN §2.6)
│   ├── recorder.py     stereo writer, event log writer thread              [reuse asr-bench storage writer pattern]
│   ├── playout.py      jitter buffer, flush-on-cancel, ear recording (spec §1.3)
│   ├── channel/        mulaw.py, resample.py, noise.py, bursts.py, muffle.py, gilbert_elliott.py, chain.py (spec §5)
│   ├── stimuli/        importer (trim, onset/end ms), overlay_engine.py (live injection at agent-onset offsets), langpack.py
│   ├── scenarios/      schema.py (Pydantic), loader.py, control_twin.py (spec §3.4)
│   ├── adapters/       base.py, mock_agent.py, openai_realtime.py, gemini_live.py, grok_voice.py, gpt_live.py, pipecat_cascade.py, dograh_stack.py (harness-as-carrier over Dograh's Twilio-protocol WebSocket; D2), selfhosted_fd.py
│   ├── tools/          mock_backends/ (one per domain), latency_injector.py, state.py (canonical DB snapshot + hash), server.py (HTTP, per-call token; Dograh workflows call it), client.py (in-process path for hosted adapters)
│   ├── simulator/      asr_stream.py (reference ASR), caller_llm.py, caller_tts.py, turn_policy.py, validation_gate.py (spec §7)
│   ├── refasr/         pinned local ASR wrapper + floor_calibration.py (spec §4.3)
│   ├── metrics/        names.py (the only metric names), activity.py (RMS frames, inactivity runs; one implementation for live and post-run), latency.py (spec §1.5 names only), floor_control.py, task.py, entities.py, cost.py, diagnostics.py (TOR)
│   ├── classifiers/    respond_resume.py, success_claim.py, recovery_request.py, validate.py (kappa, flip rate)
│   ├── stats/          wilson.py, paired_bootstrap.py, planner.py           [reuse asr-bench metrics/stats.py]
│   ├── load/           scheduler.py, sysmon.py                             [reuse asr-bench load/]
│   ├── transport/      live_stream.py, twilio_media.py (one Media Streams codec for dograh_stack and phone_path), phone_path.py (two trunk legs + bridge), loopback_baseline.py
│   ├── report/         text.py (from M2), html.py (with --public), clips.py, caveats.py, verify.py, run_card.py
│   ├── calibrate.py    mock-agent calibration (spec §1.4) + reference-ASR RTF stage (D4)
│   ├── storage.py
│   └── cli.py
├── tests/  unit/, integration/ (fake provider, tool server), golden/ (M1, M2, M4, M7 replays under the virtual clock, asserted exactly; D3)
├── docs/   reference-notes.md, licenses.md, providers.md, methodology.md, decisions.md
├── reference/   (cloned read-only, gitignored)
└── results/     (gitignored)
```

Reuse from `C:\Users\Lenovo\research\asr-bench` by copying modules with attribution in the file header, not by importing across repos. The files that exist there: `src/asr_bench/clock.py` (perf_counter clock), `pipeline/loop.py` (precise selector loop, `sleep_until_ns`), `metrics/stats.py` (`ci`, `paired_diff`, clustered bootstrap), `load/scheduler.py` (`open_loop`), `load/sysmon.py`, `storage.py` (writer thread, `canonical_json`, `config_id`), `metrics/normalize.py`, `report/calibrate.py` and `report/verify.py` patterns, and their tests `tests/test_normalize.py`, `test_stats.py`, `test_scheduler_storage.py` (copy the tests too; they must pass unchanged).

## 3. Reference repos to read first (Milestone 0)

Clone with `--depth 1` into `./reference/`. For each: read, summarize the harness design and every metric definition in `docs/reference-notes.md`, and **record the license in `docs/licenses.md` before reusing any code or data**. Where a license is not clearly permissive for reuse, read only; do not copy.

| Repo | Read for | License |
|---|---|---|
| `github.com/DanielLin94144/Full-Duplex-Bench` | TOR thresholds (the spec requires the exact rule from the code, not the paper), v1.5 reaction labels, v3 latency breakdown and tool scenarios | **check** |
| `github.com/sierra-research/tau2-bench` | voice simulator design, audio effects parameters (noise, bursts, Gilbert-Elliott, muffling), database end-state grading, domain data for optional import | **check** |
| `github.com/SesameAILabs/turnbench` | dual-channel annotation format for v1.1; signed latency convention | **check** |
| `github.com/pipecat-ai/stt-benchmark` | VAD-anchored timing, service adapters | MIT, verified 2026-11-02 in the ASR harness notes; re-check |
| `github.com/pipecat-ai/pipecat` | cascade baseline components, interruption events, Twilio/Telnyx transports | **check** (believed BSD-2-Clause; confirm) |
| Silero VAD | offline cross-check VAD at 8 kHz | **check** (believed MIT; confirm) |

Also read and summarize: the Artificial Analysis speech-to-speech methodology page (TTFA definition, forced endpointing, how the simulated customer hears the model), and Twilio Media Streams or Telnyx media documentation for frame format and sample rate on the trunk.

## 4. Milestones, acceptance tests, gates

Costs marked $0 must not call any paid API. Approve-first items print projected cost and wait.

### M0 — Reference notes and license audit ($0)
Deliver `docs/reference-notes.md`, `docs/licenses.md`, `docs/providers.md` (with confirmed facts for every provider you can reach without spending), `docs/decisions.md` (a copy of the Decision Log with a column for build-time notes).
Also `docs/dograh-ingress.md` (BUILD_PLAN M0-T05): how a `twilio`-mode workflow run is created without a carrier webhook, the first WebSocket messages Dograh's Twilio handler expects, whether a capability token is required, and how a workflow calls an external HTTP tool. If run creation or the tool path cannot be scripted, write "FALLBACK B required" (in-process pipeline with a `components_only` flag) and stop.
**Accept:** every repo in §3 has notes and a recorded license; every provider in spec §2.1 has an entry or an explicit "not confirmed, blocked on: …"; the five ingress answers have file:line evidence.
**STOP: show and wait.**

### M1 — Clock, recorder, playout, channel model, calibration ($0)
Build `clock` (real and **virtual** implementations, spec §1.1; every wait goes through `Clock.sleep_until_ns` or `Clock.idle`, events ordered by (t_ns, seq), a cancel at a frame boundary applies before the frame), `recorder`, `playout` (queue with logged depth, flush on cancel, no invented flush; spec §1.3), `channel/` (seeded in both directions), `metrics/activity.py` and the event-anchored latencies in `metrics/latency.py` (moved here from M2 because calibration needs them), `adapters/mock_agent.py`, `runner.py`, `calibrate`.
**Accept (unit):** μ-law encode–decode round trip within quantization error; Gilbert-Elliott average loss 2.0% ± 0.3 over a simulated 10 minutes with burst lengths in the configured range; noise bed achieves the target SNR within ±1 dB measured on speech-active frames; muffling applied to 20% ± 5% of utterances over 200 utterances; chain order matches spec §5 (noise is present in the decoded signal but packet-loss gaps contain repeated frames, not noise); playout queue depth is logged per frame and `backlog_ms` is available at any timestamp.
**Accept (calibration):** mock agent programmed with first-audio 400 ms, stop 250 ms after an interruption, tool call at +1,200 ms: event-anchored timings within ±5 ms, audio-anchored within ±20 ms + jitter buffer; event-loop lag p99 < 5 ms; RMS/Silero boundary disagreement < 40 ms.
**Accept (golden):** under the **virtual clock** with fixed seeds, a checked-in mock run reproduces its stored recording hash and event log exactly. (Real-clock runs are not expected to be byte-identical.)
**Accept (runner):** an adapter that raises mid-call yields outcome `transport_error`, a readable recording and flushed rows, and the next call still runs.
**STOP: show and wait.**

### M2 — Scenarios, overlays, scripted callers, Track A metrics (approve TTS stimulus cost first)
Build scenario schema and loader (caller steps, overlay anchors), the en-US language pack (public lists; the private held-out extension is a human deliverable, see M2b), stimulus importer, the live overlay engine (triggers on agent onset from played-frame activity; logs scheduled vs actual offset; tags `not_injected` when the agent utterance ends before the offset), control twins with caller-step alignment and per-metric validity windows (spec §3.4), `metrics/activity.py` (utterance segmentation by 500 ms inactivity runs), `metrics/latency.py` (only the names in spec §1.5), `metrics/floor_control.py`, `metrics/diagnostics.py` (TOR with FDB's exact rule from the reference code), `classifiers/respond_resume.py`.
**Accept:** with mock agents programmed as (a) always stops in 300 ms, (b) never stops, (c) stops on everything including backchannels, (d) short replies that end at 1.2 s, (e) emits a cancel event then restarts the same sentence 20 ms later with no 500 ms gap: Yield @1.0 s = 1.0 / 0.0 / 1.0 / excluded as `ambiguous_end` / 0.0; Hold-while-speaking = 1.0 / 1.0 / 0.0 / excluded / 1.0; (d) at offset 2.0 s is tagged `not_injected`; contested items appear only in the contested table; actual offsets within 300 ms of schedule on the mock or the event is tagged contested; `T_respond_ear` is computed from utterance boundaries without the classifier.
**Accept (golden M2):** the overlay + control-twin mock run reproduces its stored Track A table exactly (D3).
**STOP: show a 5-call mock smoke text report (`report/text.py`, built in this milestone) and wait.**

### M2b — Human inputs gate (no code)
Before M4 can be accepted, a human at Dograh must deliver: the 40 private and 20 public scenario YAML files (you build a scenario template generator and a validator; you do not author private scenarios); the private held-out pattern and token extensions (spec §11); and the pre-registration of every threshold. You build `cli preregister` (hashes configs, pack, thresholds; writes a timestamped record) and `cli label` (a terminal labeling tool that plays a clip, shows the reference-ASR text and records a label from two labelers) so that the 200-item validation sets can be collected later.
**STOP: list exactly what is missing and wait.**

### M3 — First live adapter: gpt-realtime over live stream (approve ≈ $5)
Read the current docs, fill `docs/providers.md`, build `adapters/openai_realtime.py` with recorded-event test fixtures (capture real events once, replay in tests). Confirm whether μ-law 8 kHz input is accepted natively and record the delivery path.
Build `metrics/cost.py` (priced config with URL and date) and `budget.py` (reservation at call admission) first; `cli run` prints the projection and waits for `--yes`.
**Accept:** 5 Track A scripted calls under C0 produce a report with every Track A metric populated, calibration passed, and actual cost printed; one call with the provider socket dropped at 30 s yields `transport_error` with its recording and the run continues.
**STOP: show results and cost; wait.**

### M4a — Reference-ASR candidate table ($0; model downloads need approval)
Build `refasr/engine.py` and one wrapper per candidate; produce `docs/refasr-candidates.md` (license, size, streaming support, word timestamps, WER on a 20-item floor-mini set, RTF, partial-latency p90). A human picks the model (§6 item 3); then pin it.
**STOP: show the table and wait for the choice.**

### M4 — Mock tools, Track C, reference ASR, simulator (approve simulator cost first)
Add the reference-ASR stage to `calibrate` (D4: partial-latency p90 and real-time factor on a 60 s checked-in clip; Track C runs are refused when p90 > 300 ms or RTF > 0.5, both [Proposed] thresholds). Build the five domain backends with canonical state hashing and **result anchors**, served by `tools/server.py` (HTTP, per-call token) and `tools/client.py`, the latency and failure injector, `refasr/` with a pinned local model, the simulator with the turn policy, the **tool-wait patience rule** (spec §7: no floor-taking while a tool is pending and for 1.5 s after its result) and the validation gate, `metrics/task.py` (including `T_tool_to_info_ear` and `unscripted_overlap` tagging), `classifiers/success_claim.py`, `classifiers/validate.py` (kappa, flip rate, pattern recall).
**Accept:** a scripted "perfect agent" mock passes every public-dev scenario (Pass@1 = 1.0); a mock that writes the pre-correction value scores stale-action = 1.0 on correction scenarios; a mock that claims success without a write scores unsupported-claim = 1.0 and the metric carries the `classifier` marker; a mock that is silent for 5 s during a 5 s tool wait triggers no simulator speech and scores 5 s of in-tool dead air; a mock that plays filler for the whole wait and then states the result scores `T_tool_to_info_ear` equal to the post-result delay, not ~0; `T_caller_think` is logged for every simulator turn; simulator mishearing rate is computed where the mock emits a transcript; the validation gate regenerates an off-goal caller and reports the rate; `unmatched_control` is tagged when a control twin's step sequence diverges; the diagnostic `anchor_offset_in_utt_ms` is reported beside `T_tool_to_info_ear` (D6); regeneration is a whole-trial rerun capped at `sim_regen_max = 2` with every attempt kept and costed; the Track C mock golden reproduces its task table exactly (D3).
**STOP: show and wait.**

### M5 — Track D entities and the reference-ASR floor ($0 build; approve floor-set TTS cost)
Extend the normalizer (NATO alphabet, spelled letters, email tokens), build `metrics/entities.py` (first attempt = earlier of first tool call and first read-back), `classifiers/recovery_request.py`, and `refasr/floor_calibration.py` covering **both** entity read-backs and `required_spoken` confirmations.
**Accept:** normalizer fixtures pass (spoken vs written IDs, phones, amounts, dates); the floor set of ≥ 200 synthesized items through C0 produces per-type entity floors and a confirmation floor stored in the run card; entity metrics and Pass@1 flag results within the floor; a mock that reads back a wrong value and later writes the correct one scores first-attempt wrong, final correct.
**STOP: show the floor table and wait.**

### M6 — Remaining adapters, one at a time (approve ≈ $5 each)
Order: Pipecat cascade baseline (spec §2.2), Dograh stack, Gemini Live, Grok Voice, GPT-Live-1, self-hosted full-duplex (optional; needs GPU approval). For each: docs confirmed, recorded-event fixtures, a 5-call smoke, cost printed. If a provider lacks a cancel/truncate event, record it; do not fake one.
**STOP after each adapter.**

### M7 — Statistics, cost, planner, report, verify ($0)
Build `stats/`, `metrics/cost.py` with priced system configs (price source URL and date per entry; leave `usd: null` and stop if a price page cannot be found), the network baseline (`cli netcheck`), `report/`, `cli planner`, `cli verify`.
**Accept:** Wilson and paired bootstrap match hand-computed fixtures; the planner reproduces spec §9.2 exactly; the golden mock run's report is byte-identical on re-render and `verify` diff is 0; every spec §10 item renders, including playout penalty and `backlog_ms`; caveats fire on a fixture with low n, `ambiguous_end`, `not_injected`, `unmatched_control`, `slow_caller`, `unscripted_overlap`, an unvalidated classifier and a missing cancel event.
**STOP: show the golden report and wait.**

### M7b — Classifier validation (human labels; no spend)
Run `cli label` sessions with two human labelers on the three 200-item sets; run `classifiers/validate.py`.
**Accept:** kappa, human–human agreement and pattern recall stored in the run card; any classifier below 0.7 stays marked `classifier` and out of headline rows.
**STOP: show the validation table and wait.**

### M8 — Core run: Dograh stack + one reference S2S (approve projected cost; spec estimates $300–700 per system)
`cli preregister` both configs and all thresholds before the run. Run Core on the **whole** `private-test` pack.
**Accept:** run card complete; every headline metric has n and CI; the planner's power notes appear; calibration passed on the instance; harness-invalid calls (transport error, pacing fail) ≤ 2 %; every attempt preserved; `ambiguous_end` / `not_injected` / `unmatched_control` / `contested` rates reported per system (they are expected outcomes, never a failure); pre-registered coverage n met.
**STOP: present the report; wait for the decision on Full.**

### M9 — Phone path and concurrency (approve trunk and run cost)
Build `transport/phone_path.py` and `loopback_baseline.py`; run the 10-scenario subset and the c = 8 Track A pass.
**Accept:** loopback baseline stored; sign-agreement table rendered; 429/error rates per level reported.
**STOP.**

### M10 — Full run (approve projected cost; spec estimates $4k–8k for seven systems)
Only after Core has run at least twice and its results were reviewed.

## 5. Conventions the code must follow

- Metric names are exactly the spec's names; no metric is created without a spec section reference in its docstring.
- Caller-side anchors come from synthesized timestamps, never from ASR.
- Agent activity at the ear comes from played frames (RMS), never from VAD; Silero is a cross-check only.
- Every rate carries n; every percentile carries n and a completion rate; p99 is withheld under n = 1,000.
- Paired comparisons use the pair key (scenario_id, trial_index, condition); overlay events pair on (scenario_id, overlay_id, trial_index); clusters are scenario_id; a pair is dropped when either side is invalid for that metric and the drop count per system is reported.
- Nothing private (scenario YAML, audio, pattern extensions) is ever committed to this repo; a unit test enforces it.
- No comparison is labeled "better" or "distinguishable" except by the paired-difference CI.
- Judged or classifier-assisted numbers carry a `judged` or `classifier` marker in every table and are absent from headline rows until validation (kappa ≥ 0.7) is stored in the run card.
- Runs with different scenario-pack, langpack, normalizer, reference-ASR or simulator hashes never aggregate.

## 6. What you must never decide on your own

Stop and ask when any of these comes up. Record the answer in `docs/decisions.md`.

1. Any paid call, GPU hour or trunk minute before approval; any change to `max_usd`.
2. Which third parties may receive audio (new TTS, hosted ASR, a new provider).
3. The reference-ASR model choice and any change to it after the floor is measured.
4. Any numeric threshold in the spec (all are [Proposed] unless labeled otherwise: RMS −45 dBFS, 500 ms inactivity run, 60 ms jitter buffer, 700 ms end of turn, U(300, 800) ms floor delay, 1.5 s tool-wait patience, budgets 0.5/1.0/2.0 s, 2.0 s hold window, 5 s response window, 6 s E-END silence, 8 min timeout, 20 s stall, 300 ms offset tolerance, and the rest). Implement them as config with the spec's defaults; never change a default silently or per system.
5. Adding, removing or relabeling an overlay class, a Y-CMD or H-BC token, or moving an item into or out of the contested bucket.
6. Any metric not in the spec, any new latency name, or any composite score.
7. The scenario content for `private-test` (domains, counts, which production workflows define "Dograh stack") and the private held-out pattern extensions. You build the tooling; humans author these.
8. Schema changes after results exist; deleting results; comparing across pack versions.
9. Faking or inferring a provider event that the provider does not emit.
10. Tuning any system's configuration (the iteration budget is a human decision; you log iterations, you do not choose them).
11. Publishing or sharing any result outside the repo.
12. Using Deepgram or any Dograh production component as the reference ASR.
