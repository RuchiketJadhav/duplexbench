# BUILD_PLAN — Dograh Duplex Bench, task-level plan for a small-model executor

Date: 2026-11-04. Produced by the engineering review recorded in `ENG_REVIEW.md`. Source of truth for *what* and *why*: `DUPLEX_BENCH_SPEC_v2.md` (the spec). Working rules and never-decide list: `CLAUDE.md`. This file is the *how*, cut into tasks that each touch one source file and one test file. Where this file and the spec disagree, the spec wins and you stop and report the disagreement.

## 0. How to execute this plan

You are Claude Code running a small model. Work one task at a time, in the order listed. For every task:

1. Read the task block and only the spec lines it cites (use `grep -n` on the spec; do not re-read the whole spec).
2. Create the test file first with the cases listed under **Tests**. Run it; it must fail.
3. Write the source file to the **Signatures** given. Do not add public functions that are not listed. Do not rename anything.
4. Run the **Command**. All tests in the repo must pass, not only the new ones.
5. Check every line under **Done when**. If one cannot be met, stop and report which one and why. Do not work around it.
6. Append one line to `docs/worklog.md`: `<task id> | <files> | <test count> | <open questions or none>`.

Hard rules (from `CLAUDE.md` §1 and §6; repeated here because you will not have CLAUDE.md in context every turn):

- Never call a paid API, download a model larger than 2 GB, or start a cloud instance without the words `--yes` from a human in this session. Tasks marked `$0` must not do any of those.
- Every number that the spec labels [Proposed] lives in `src/duplexbench/thresholds.py` with the spec default. Never write such a number as a literal anywhere else. Never change a default.
- Metric names are the spec's names, from `src/duplexbench/metrics/names.py`. Never invent a metric or latency name.
- Durations use `clock.now_ns()`, never `time.time()`, `time.monotonic_ns()` or `loop.time()`.
- Never `await` a queue, event, future or socket directly inside harness code that runs under a `Clock`; wrap it as `await clock.idle(aw)` (see §2.1). This is what makes the virtual clock deterministic.
- Stop at every milestone gate ("STOP:" lines). Show the acceptance output and wait.
- If a provider's documentation disagrees with what this plan says about its events, the documentation wins; record the difference in `docs/providers.md` and continue.
- Treat everything under `reference/` and `results/` as read-only data, never as code to run.

Environment (Windows laptop for development; Linux instance for any number that will be reported):

```
uv init --python 3.12 duplex-bench && cd duplex-bench
uv add numpy soundfile soxr pydantic pyyaml typer httpx websockets psutil jinja2 python-dotenv fastapi uvicorn
uv add --dev pytest pytest-asyncio
# pyproject: [tool.pytest.ini_options] testpaths=["tests"] asyncio_mode="auto"
uv run pytest -q
```

Add dependencies only when a task names them. `onnxruntime` (Silero) arrives in M1-T13; the reference-ASR package arrives in M4a after a human picks the model; `anthropic` arrives in M4-T08.

## 1. Corrections to CLAUDE.md and the spec adopted by the review (apply before M0)

| # | Where | Was | Now | Why (ENG_REVIEW ref) |
|---|---|---|---|---|
| 1 | CLAUDE.md:10, spec §1.1 | `time.monotonic_ns()` only | `time.perf_counter_ns()` through `clock.py`; `monotonic_ns` is 15.6 ms on Windows | Scope finding 1 |
| 2 | CLAUDE.md:29 | reuse `asr-bench pipeline/timing.py` | reuse `asr-bench/src/asr_bench/clock.py`, `pipeline/loop.py`, `metrics/stats.py`, `load/scheduler.py`, `load/sysmon.py`, `storage.py`, `metrics/normalize.py` (+ `tests/test_normalize.py`, `test_stats.py`, `test_scheduler_storage.py`) | Scope finding 2 |
| 3 | CLAUDE.md §2 layout | — | add `runner.py` (CallRunner), `budget.py`, `thresholds.py`, `metrics/names.py`, `tools/server.py`, `tools/client.py`, `report/text.py`, `transport/twilio_media.py`, `simulator/prompts/` | Codex #3, #4, #5; C1–C3 |
| 4 | CLAUDE.md:27, spec §11 | private YAML committed in the harness repo | private pack lives in a second private repo, mounted at `DUPLEX_PRIVATE_DIR`; the harness repo never contains a private path | D5 |
| 5 | CLAUDE.md:35 | `dograh_stack.py` (no transport) | harness-as-carrier: speaks Twilio Media Streams into Dograh's telephony WebSocket; M0 confirms run creation; fallback in-process with `components_only` flag | D2 |
| 6 | CLAUDE.md M1/M2/M3 | activity+latency in M2, report in M7, cost in M7 | `metrics/activity.py` and event-anchored latency in M1; `report/text.py` in M2; `metrics/cost.py` projection + `budget.py` reservation in M3 | Codex #4 |
| 7 | CLAUDE.md M2, M4 | no golden | golden replays at M1, M2, M4, M7 | D3 |
| 8 | spec §1.4, CLAUDE.md M4 | — | calibration includes a reference-ASR stage; Track C runs refused when partial p90 > 300 ms or RTF > 0.5 | D4 |
| 9 | CLAUDE.md M8 | "no invalidity flags" | "calibration passed; harness-invalid calls ≤ 2 %; all attempts preserved; exclusion rates reported; pre-registered coverage n met" | Codex #8 |
| 10 | spec §7 | regeneration unspecified | whole-trial rerun, `sim_regen_max = 2` [Proposed]; discarded trials kept with `regenerated`; third failure = `validation_failed` outcome | Codex #10 |
| 11 | spec §4.2 | — | diagnostic `anchor_offset_in_utt_ms` beside `T_tool_to_info_ear` (and first read-back) | D6 |
| 12 | spec §9.1 | pairing implicit | pair key `(scenario_id, trial_index, condition)`; events `(scenario_id, overlay_id, trial_index)`; cluster `scenario_id`; a pair drops when either side is invalid for that metric; drop counts per system reported | Codex #9 |

## 2. Contracts every task depends on

Write these exactly. Later tasks import them; changing a signature later means re-running every test that imports it.

### 2.1 `src/duplexbench/clock.py`

```python
class Clock(Protocol):
    def now_ns(self) -> int: ...                      # ns since the run origin; starts at 0
    async def sleep_until_ns(self, t_ns: int) -> None: ...
    async def idle(self, aw: Awaitable[T]) -> T: ...  # await something that is not time
    def task(self, coro) -> asyncio.Task: ...          # spawn a task the clock accounts for

class RealClock:      # perf_counter_ns relative to origin; sleep via loop.sleep on the precise loop
class VirtualClock:   # deterministic; see rules below
```

VirtualClock rules: it keeps `_now`, a heap of `(t_ns, seq, future)`, and `_active` (number of accounted tasks that are neither sleeping nor idle). `sleep_until_ns` pushes to the heap, decrements `_active`, awaits the future, increments on wake. `idle` decrements around the await. After every decrement, `call_soon(_maybe_advance)`; `_maybe_advance` pops the heap only when `_active == 0`; it sets `_now = t_ns` and resolves every future with that same `t_ns` in heap order (seq). If `_active == 0` and the heap is empty, raise `DeadlockError` (a test asserts this). Events that happen "at the same time" are ordered by `seq`, which is assigned at registration. A cancel event scheduled at the same `t_ns` as a frame play applies before the frame (cancel registered first by the runner; test M1-T06 pins this).

The precise loop: copy `asr-bench/src/asr_bench/pipeline/loop.py` to `src/duplexbench/_loop.py` (header attribution). `RealClock.sleep_until_ns` = its `sleep_until_ns`.

### 2.2 Frames and audio

- Internal PCM is `numpy.int16`, mono, 8,000 Hz, 160 samples per 20 ms frame. μ-law frames are `bytes` of length 160. The wideband diagnostic condition uses 16,000 Hz and 320-sample frames; every function takes `rate` explicitly.
- `Frame = dataclass(t_ns: int, seq: int, src: Literal["caller","agent"], pcm: np.ndarray | None, mulaw: bytes | None)`.
- Recording: `results/<run_id>/<call_id>.wav`, 2 channels, 16-bit, 8 kHz (16 kHz under W): left = caller as delivered (decoded), right = ear.

### 2.3 Adapter contract — `src/duplexbench/adapters/base.py`

```python
EventKind = Literal["session_open","first_audio_byte","audio","cancel_or_truncate",
                    "output_transcript","tool_call","usage","error","reconnect","closed"]

@dataclass
class AdapterEvent:
    kind: EventKind
    t_ns: int                 # clock.now_ns() when the harness observed it
    provider_event: str       # raw provider event name, "" for harness-generated
    payload: dict             # audio: {"pcm": np.ndarray int16, "rate": int} ; tool_call: {"id","name","arguments"} ; usage: {...raw...}

@dataclass
class AdapterCaps:
    cancel_event: bool        # provider emits a cancel/truncate the harness can flush on
    native_mulaw: bool        # accepts μ-law 8 kHz bytes without transcoding
    output_transcript: bool
    usage_fields: bool

class Adapter(ABC):
    name: str; caps: AdapterCaps; config: dict         # config hashed to config_id
    async def open(self, call: "CallContext") -> None
    async def send_frame(self, mulaw: bytes, t_ns: int) -> None
    def events(self) -> AsyncIterator[AdapterEvent]
    async def tool_result(self, tool_call_id: str, result: dict) -> None
    async def close(self) -> None
```

An adapter never raises out of `events()`; it yields `error` and then `closed`. Audio events carry PCM at the provider's rate; the runner resamples. `first_audio_byte` is emitted by the base class on the first `audio` event after each caller turn end (the runner tells the adapter the turn ended via `call.turn_ended(t_ns)`), so adapters do not implement it.

### 2.4 Event log — `src/duplexbench/storage.py` (copy asr-bench `storage.py`; change the schema)

Tables (all with `run_id TEXT, call_id TEXT`):

- `runs(run_id PK, started_ns, config_json, hashes_json, calibration_json, run_card_json)`
- `calls(call_id PK, run_id, scenario_id, trial, condition, system_id, is_control, outcome, started_ns, ended_ns, wav_path, usd_projected, usd_actual, flags_json)`
- `events(run_id, call_id, t_ns, seq, kind, src, payload_json)` — stimulus, simulator, adapter, tool, harness-health events; index `(call_id, t_ns, seq)`
- `playout(run_id, call_id, t_ns, frame_idx, rms_dbfs, active, queue_depth, backlog_ms, flushed)` — one row per played frame; index `(call_id, t_ns)`
- `overlays(run_id, call_id, overlay_id, cls, scheduled_ns, actual_ns, onset_ns, end_ns, tags_json)`
- `tool_calls(run_id, call_id, tool_call_id, name, args_json, start_ns, result_ns, injected_latency_ms, injected_failure, result_json)`
- `sim_turns(run_id, call_id, turn_idx, text, tts_words_json, send_ns, think_ms, speculative, discarded)`
- `observations(run_id, call_id, metric, scenario_id, trial, condition, system_id, event_id, value, valid, tags_json)` — written by `cli score`, never by the live loop

`Store.put(table, row)` is non-blocking (writer thread, batches of 100, WAL); `await store.flush()` drains. Keep asr-bench's `canonical_json`, `config_id`.

### 2.5 Scenario schema — `src/duplexbench/scenarios/schema.py` (Pydantic v2)

```python
class CallerStep(BaseModel): id: str; text: str; clip: str | None; speech_onset_ms: int | None; speech_end_ms: int | None; pause_ms: int | None; resumption_text: str | None
class Overlay(BaseModel): id: str; cls: Literal["Y-INT","Y-CMD","H-BC","H-SIDE","H-BG","H-TIC","P-PAUSE","E-END"]; anchor_step: str; placement: Literal["during_agent","in_pause"]; offset_ms: int; clip: str | None; text: str; level_db: float; response_anchors: list[str]; resumption_ms: int | None; voice_id: str | None
class ToolDef(BaseModel): name: str; kind: Literal["read","write"]; latency_ms: int; failure: Literal["none","timeout","error","partial"]; result_anchors: list[str]
class Scenario(BaseModel): id: str; version: str; split: Literal["public-dev","private-test"]; domain: str; track: Literal["A","C","D"]; source: Literal["scripted","simulated","recorded"]; speaker_id: str | None; caller_voice: str; speaking_rate: float; persona: str | None; goal: str | None; facts: dict; steps: list[CallerStep]; overlays: list[Overlay]; tools: list[ToolDef]; expected_state: dict; required_spoken: list[dict]; entities: list[dict]; policy: dict; conditions: list[str]; trials: int; control_twin: bool
```

`loader.load_pack(dir) -> Pack(scenarios, hash)`; the hash is `sha256` of the canonical JSON of every scenario sorted by id plus the sha256 of every referenced audio file.

**Note (2026-11-05, integration).** Resumption clips follow the convention `<clip stem>_resumption<ext>`, next to the clip they resume: a step with `pause_ms` resumes from `audio/<step clip stem>_resumption.wav` (clips key `<step>/resumption`), and a wait-class Y-CMD overlay with `resumption_ms` resumes from `audio/<overlay clip stem>_resumption.wav` (clips key `<overlay>/resumption`). The resolver is `scenarios/loader.py` `resumption_clip_path` / `clip_sources`; `duplexbench run` requires the step resumption files and reads overlay resumption files when present (the overlay engine raises if a wait-class Y-CMD needs one that is missing). `load_pack` does not require them; a resumption file that exists enters the pack hash like any referenced clip, and one that does not is skipped, so existing fixtures (track_a_002 ships none; the golden M2 reuses `a002_s2.wav`) still load with unchanged hashes. `ToolDef` also gained optional `anchor_step: str | None` (the caller step carrying the tool's arguments; `metrics/task.py` `t_tool_offset`); the loader checks it names a step.

### 2.6 Thresholds — `src/duplexbench/thresholds.py` + `configs/thresholds.yaml`

One Pydantic model, every field `(value, source)` where source ∈ {Proposed, Fact}. Fields and defaults (spec section in brackets): `rms_active_dbfs = -45 [1.2]`, `inactivity_run_ms = 500 [1.2]`, `jitter_buffer_ms = 60 [1.3]`, `frame_ms = 20 [1.3]`, `calib_event_tol_ms = 5 [1.4]`, `calib_audio_tol_ms = 20 [1.4]`, `loop_lag_p99_ms = 5 [1.4, Fact]`, `pacing_slip_p99_ms = 10 [1.4, Fact]`, `silero_disagree_ms = 40 [1.4]`, `yield_budgets_ms = [500,1000,2000] [4.1]`, `yield_headline_ms = 1000`, `hold_window_ms = 2000 [4.1]`, `hold_also_ms = 1000`, `response_windows_ms = [2000,5000] [4.1]`, `respond_jaccard = 0.5 [4.1]`, `leakage_window_ms = 10000`, `contested_offset_ms = 300 [3.3]`, `contested_pause_ms = 1500 [3.3]`, `overlay_offsets_ms = [500,2000] [3.2]`, `wait_resume_ms = [3000,6000] [3.2]`, `pause_ms = [600,1500] [3.2]`, `eend_silence_ms = 6000 [3.2]`, `dead_air_gap_ms = 2000 [1.5]`, `call_timeout_ms = 480000 [4.2]`, `stall_ms = 20000 [4.2]`, `tool_latency_buckets_ms = [0,2000,5000] [4.2]`, `repeat_prompt_words = 4 [4.2]`, `sim_eot_ms = 700 [7]`, `sim_speculate_ms = 300 [7]`, `sim_floor_delay_ms = [300,800] [7]`, `sim_patience_ms = 1500 [7]`, `sim_slow_caller_p90_ms = 1500 [7]`, `sim_regen_max = 2 [7, D-review]`, `sim_regen_review_rate = 0.10 [7]`, `refasr_partial_p90_ms = 300 [1.4, D4]`, `refasr_rtf_max = 0.5 [1.4, D4]`, `kappa_min = 0.7 [8]`, `flip_rate_max = 0.02 [8]`, `bootstrap_resamples = 1000 [9.1]`, `p99_min_n = 1000 [4.4]`, `holm_max_comparisons = 6 [9.1]`, `harness_invalid_max = 0.02 [M8]`, `ge_p = 0.0034`, `ge_r = 0.1667`, `ge_loss_in_bad = 1.0 [5]`, `noise_snr_db = 15`, `noise_drift_db = 3 [5]`, `burst_per_min = 1.0`, `burst_len_ms = [150,400]`, `burst_level_db = -10 [5]`, `muffle_share = 0.20`, `muffle_cutoff_hz = 1200 [5]`, `side_level_db = -6`, `bg_level_db = -12 [3.2]`, `tuning_iterations = 10 [2.3]`.

Test `tests/unit/test_thresholds_guard.py`: walks `src/duplexbench/**/*.py` except `thresholds.py` and asserts that none of the literal strings `-45`, `500`, `60`, `700`, `300`, `800`, `1500`, `2000`, `5000`, `6000`, `480000`, `20000`, `0.7`, `0.02`, `1000` appear as standalone numeric literals outside comments and tests (regex `(?<![\w.])<num>(?![\w.])`). Expect false positives; whitelist by a `# threshold-ok` trailing comment only with a reason.

### 2.7 CallRunner — `src/duplexbench/runner.py` (Codex #3)

```python
@dataclass
class CallContext: run_id: str; call_id: str; scenario: Scenario; trial: int; condition: Condition; system_id: str; is_control: bool; clock: Clock; store: Store; tool_token: str
async def run_call(ctx: CallContext, adapter: Adapter, caller: CallerSource, overlays: OverlayEngine | None, playout: Playout, channel: ChannelChain, tools: ToolClient | None, budget: Budget) -> CallResult
```

Owns four accounted tasks: `_send_caller` (paces 20 ms frames from `caller` through `channel.forward` to `adapter.send_frame`; marks turn ends), `_recv_agent` (drains `adapter.events()`; audio → `channel.back` → `playout.enqueue`; `cancel_or_truncate` → `playout.flush(t)`; `tool_call` → `tools.call(...)` then `adapter.tool_result`; logs every event), `_play` (`playout.run()`: pops a frame every 20 ms on the clock, writes ear audio, RMS, activity, queue depth; publishes `onset`/`stop` to subscribers), `_overlays` (subscribes to onsets, schedules injections through `caller.inject`). Ends on: E-END silence budget reached, `call_timeout_ms`, `stall_ms`, adapter `closed`, simulator `<END>`, or budget refusal before start. Any exception in any task → `error` event, outcome `transport_error`, cancel the other tasks, `await store.flush()`, close the WAV, return. `CallResult(outcome, usd_actual, flags)`.

### 2.8 Tool server — `src/duplexbench/tools/server.py` + `client.py` (Codex #5)

FastAPI app: `POST /tools/{call_token}/{tool_name}` body = arguments → applies the scenario's injected latency and failure, runs the domain backend bound to `call_token`, returns the result, and records a `tool_call` event with `start_ns`/`result_ns` on the harness clock. `GET /state/{call_token}` → canonical state JSON + hash. `ToolClient.call(name, args)` is the in-process path hosted adapters use; it hits the same backend object, so the event rows are identical. Dograh's workflow is pointed at `http://<harness>/tools/<call_token>/` (M0-T05 confirms how).

### 2.9 Metric observation — `src/duplexbench/metrics/base.py`

```python
@dataclass
class Observation: metric: str; call_id: str; scenario_id: str; trial: int; condition: str; system_id: str; event_id: str | None; value: float | None; valid: bool; tags: tuple[str, ...]
class CallRecord:   # loaded by cli score from the store + wav; read-only
    events: list[Event]; playout: np.ndarray (structured); overlays: list[OverlayRow]; tool_calls: list[ToolRow]; sim_turns: list[SimRow]; wav_path: Path; scenario: Scenario; twin: "CallRecord | None"
```

Every metric module exposes `def observe(rec: CallRecord, th: Thresholds) -> list[Observation]`. Values are `None` with `valid=False` and a tag when undefined (`not_stopped`, `ambiguous_end`, `not_injected`, `unmatched_control`, `contested`, `slow_caller`, `unscripted_overlap`, `classifier`, `diagnostic`).

### 2.10 Budget — `src/duplexbench/budget.py` (Codex #4)

`Budget(max_usd: float)`, `reserve(projected_usd) -> Reservation` (raises `BudgetExceeded` if `settled + reserved + projected > max_usd`), `settle(reservation, actual_usd)`. The runner reserves before `adapter.open` and settles after `close`. `cli run` prints the projection for the whole run first and waits for `--yes`.

### 2.11 Metric names — `src/duplexbench/metrics/names.py`

`class Metric(StrEnum)` with exactly: `yield_rate`, `t_stop_ear`, `talk_over_share`, `respond_resume`, `t_respond_ear`, `wait_compliance`, `hold_speaking_rate`, `content_leakage_rate`, `hold_listening_rate`, `response_rate`, `t_turn_gap_ear`, `pass_at_1`, `pass_pow_3`, `stale_action_rate`, `preemptive_tool_rate`, `unsupported_claim_rate`, `policy_violation_rate`, `auth_bypass_rate`, `tool_wait_pass`, `filler_count`, `filler_s`, `dead_air_in_tool_s`, `repeated_prompt_rate`, `response_rate_per_turn`, `call_outcome`, `capture_first`, `capture_final`, `recovery_request_rate`, `recovery_success`, `readback_presence`, `readback_error_rate`, `codec_penalty`, `t_ttfa_recv`, `t_stop_model`, `t_tool_offset`, `t_tool_to_info_ear`, `t_dead_air`, `t_caller_think`, `backlog_ms`, `playout_penalty`, `tor`, `anchor_offset_in_utt_ms`, `cost_per_min`, `error_rate_at_c`, `mishearing_rate`, `regeneration_rate`. Each member has `.spec` (section string) and `.headline: bool`. `report/` raises on any name outside the enum.

## 3. Tasks by milestone

Task format: **ID — title ($ cost)**. Files. Depends on. Spec. Behavior. Signatures. Tests (file → cases with expected values). Command. Done when.

Default command for every task unless stated: `uv run pytest -q tests/unit/<test file> && uv run pytest -q`.

### M0 — Reference notes, licenses, provider facts, Dograh ingress ($0)

**M0-T01 — Repo skeleton.** Files: `pyproject.toml`, `.gitignore` (`results/`, `reference/`, `.env`, `*.wav` outside `tests/fixtures`), `.env.example` (`DUPLEX_PRIVATE_DIR=`, provider keys as empty), `src/duplexbench/__init__.py`, every package `__init__.py` from the CLAUDE.md §2 layout plus §1 row 3, `docs/worklog.md`, `src/duplexbench/README.md` with the four-task loop diagram (§2.7). Tests: `tests/unit/test_layout.py` → every path listed in CLAUDE.md §2 (as amended) exists; `tests/unit/test_no_private_files.py` → no file under `scenarios/private-test/` or `langpacks/*/private/` exists in the tree. Done when: `uv run pytest -q` passes with these two tests.

**M0-T02 — Clone references read-only.** `git clone --depth 1` each repo in CLAUDE.md §3 into `reference/`. Record `docs/licenses.md`: repo, URL, license file text's SPDX id, date, "reuse allowed: yes/no/code-only". Done when: six rows, none blank. Do not copy code in this task.

**M0-T03 — Reference notes.** `docs/reference-notes.md`: for Full-Duplex-Bench, copy the exact TOR rule from its code (file path + line + the literal thresholds); for tau2-bench, the audio-effects parameters (noise SNR, burst rate, Gilbert-Elliott p/r, muffling) with file paths; for turnbench, the annotation JSON shape; for pipecat, the interruption event class names and the Twilio serializer file path; for stt-benchmark, the VAD-anchored timing function. Each fact as `path:line — quoted text`. Done when: every row has a path and a quote; anything not found says "not found in <commit>".

**M0-T04 — Provider facts.** `docs/providers.md`: one section per system in spec §2.1 with: docs URL, date checked, input/output formats (does it accept `g711_ulaw` 8 kHz natively?), the provider event names for `first_audio_byte`, `cancel_or_truncate`, `output_transcript`, `tool_call`, `usage`; turn-detection parameters; pricing unit and price with URL; "reachable with the keys in .env.example: yes/no/unknown". Candidates to confirm for OpenAI Realtime (do not assume): `session.update{input_audio_format,output_audio_format,turn_detection{type:"server_vad",interrupt_response,create_response,silence_duration_ms,prefix_padding_ms,threshold}}`, `input_audio_buffer.append`, `response.output_audio.delta` (older `response.audio.delta`), `input_audio_buffer.speech_started`, `response.done{status:"cancelled"}`, `conversation.item.truncate`, `response.function_call_arguments.done`, `conversation.item.create{type:"function_call_output"}`, `response.output_audio_transcript.done`, `response.done.response.usage`; `output_audio_buffer.clear` is WebRTC-only. Done when: every system has a section; unknowns are written as "not confirmed, blocked on: …". No paid call.

**M0-T05 — Dograh ingress and tool path (D2, Codex #5).** Read `C:\Users\Lenovo\dograh\api\routes\telephony.py` (WS route at ~line 600, `_handle_telephony_websocket`), `api/services/telephony/providers/twilio/provider.py` (`handle_websocket` ~line 249, `initiate_call` ~line 59), `api/services/telephony/ws_auth.py`, and the workflow API-call node implementation (grep `httpx` / `aiohttp` under `api/services/workflow`). Write `docs/dograh-ingress.md` answering, with file:line evidence: (1) which API call creates a workflow run in `twilio` mode and leaves it `initialized` without a carrier webhook; (2) the exact first messages the Twilio handler expects (`connected`, `start` with `streamSid`, `customParameters`); (3) whether a capability token is required and how it is minted; (4) how a workflow node calls an external HTTP tool and how its base URL is configured per workflow; (5) how the workflow's config hash and id are read. If (1) or (4) cannot be done by API, write "FALLBACK B required" and stop. Done when: the five answers have evidence lines; the fallback decision, if any, is reported at the gate.

**M0-T06 — Decisions copy.** `docs/decisions.md`: copy `DECISION_LOG_v2.md` with an added column "build note", plus rows for D1–D6 from `ENG_REVIEW.md`. Done when: present.

**STOP M0: show licenses.md, providers.md (counts of confirmed vs blocked), dograh-ingress.md answers, and wait.**

### M1 — Clock, recorder, playout, channel, activity, latency, mock agent, calibration ($0)

**M1-T01 — Clock protocol and RealClock.** Files: `src/duplexbench/clock.py`, `src/duplexbench/_loop.py` (copied; attribution header). Depends: —. Spec: §1.1. Signatures: §2.1. Tests `tests/unit/test_clock.py`: `RealClock().now_ns()` is non-decreasing over 1,000 calls; `clock_info()["name"] == "perf_counter"`; `sleep_until_ns(now+20_000_000)` returns within 20–30 ms on the precise loop (skip the upper bound on CI). Done when: no `time.monotonic` string appears anywhere under `src/`.

**M1-T02 — VirtualClock.** Files: `clock.py` (same file, class `VirtualClock`). Tests `test_clock.py`: two tasks sleeping to 100 ms and 50 ms wake in order 50, 100 with `now_ns` equal to their targets; three sleepers to the same `t` wake in registration order; a task idle on `asyncio.Queue.get()` wrapped in `clock.idle` lets time advance; an unwrapped `queue.get()` with an empty heap raises `DeadlockError` within the test timeout; `run_until_complete` of a 10-minute virtual program finishes in < 1 s wall. Done when: all five pass.

**M1-T03 — μ-law codec.** File: `channel/mulaw.py`. Spec §5. Signatures: `encode(pcm: np.ndarray[int16]) -> bytes`, `decode(b: bytes) -> np.ndarray[int16]`, `SILENCE = b"\xff"`. Numpy implementation of G.711 μ-law (bias 0x84, 8 segments, inverted output). Tests `test_mulaw.py`: `encode(np.zeros(1,int16)) == b"\xff"`; `decode(b"\xff")[0] == 0`; for `x in [-32124..32124 step 97]`, `|decode(encode(x)) - x| <= segment_step(x)` where the step is `2**(segment+3)`; encode is monotonic non-increasing in `x` on the positive side (inverted bits); round trip of a 1 kHz sine at −20 dBFS has SNR ≥ 35 dB. Done when: no `audioop` import.

**M1-T04 — Resampling.** File: `channel/resample.py`. Signatures: `class Resampler(src_rate, dst_rate)`; `.push(pcm) -> np.ndarray` (streaming via `soxr.ResampleStream`, carries the fractional remainder, never drops samples); `.flush()`; `.delay_ms` (group delay measured once at construction by pushing an impulse); `to_frames(pcm, rate) -> list[np.ndarray]` with the remainder kept in the object. Tests `test_resample.py`: 24 kHz → 8 kHz of 1.000 s yields exactly 8,000 samples after flush; three pushes of 333 samples at 24 kHz produce the same output as one push of 999 (±1 sample); `delay_ms` is stored and < 5 ms; `band_limit` attenuates 3.6 kHz by ≥ 20 dB at 8 kHz output. Done when: passes.

**M1-T05 — Noise bed, bursts, muffle.** Files: `channel/noise.py`, `channel/bursts.py`, `channel/muffle.py`. Spec §5 (C1). Signatures: `NoiseBed(seed, snr_db, drift_db, rate).apply(pcm, speech_active_mask) -> pcm`; `Bursts(seed, per_min, len_ms, level_db, rate).apply(pcm_stream)`; `Muffle(seed, share, cutoff_hz, rate).apply(utterances: list[pcm]) -> list[pcm]`. Noise power is scaled against the mean power of frames where the clean signal's RMS > `rms_active_dbfs`; drift is a seeded random walk clipped to ±drift_db, updated per second. Tests `test_noise.py`: measured SNR on speech-active frames of a 10 s fixture = 15 dB ± 1 dB; two runs with the same seed are bit-identical; different seeds differ. `test_bursts.py`: over 60 simulated minutes, burst count ~ Poisson(60) within [40, 80]; every burst length within `[150, 400]` ms. `test_muffle.py`: of 200 utterances, 40 ± 10 are muffled; a muffled utterance has ≥ 20 dB less energy above 2 kHz. Done when: passes.

**M1-T06 — Gilbert-Elliott loss.** File: `channel/gilbert_elliott.py`. Signatures: `GE(seed, p, r, loss_in_bad).next() -> bool` (True = lost); `conceal(prev_frame) -> frame` (repeat previous; silence for the first frame). Tests `test_ge.py`: over 30,000 frames (10 min), loss = 2.0 % ± 0.3; mean burst length (consecutive losses) within [4, 9] frames; the first lost frame of a stream is concealed with μ-law silence; the same seed reproduces the same loss pattern. Done when: passes.

**M1-T07 — Channel chain.** File: `channel/chain.py`. Spec §5 chain order. Signatures: `ChannelChain(condition: Condition, seed_fwd, seed_back, rate_out=8000)`; `.forward(pcm_src, rate_src, speech_mask) -> Iterator[bytes]` (effects → band-limit/resample → μ-law → 20 ms frames → GE loss with concealment); `.back(pcm_provider, rate) -> Iterator[np.ndarray]` (resample → μ-law → frames → GE with the back seed → decode); `.delivery(adapter.caps.native_mulaw) -> "mulaw" | "pcm16@<rate>"`. Tests `test_chain.py`: with loss forced on frames 10–12, those frames equal frame 9 (repeated), while frames 13+ contain noise (order check from CLAUDE.md M1); under C0 the chain is μ-law round trip only; `Condition.W` yields 16 kHz PCM frames with no μ-law; forward and back use independent seeds (different loss patterns). Done when: passes.

**M1-T08 — Activity and utterance segmentation.** File: `metrics/activity.py`. Spec §1.2. Signatures: `frame_rms_dbfs(pcm) -> float`; `is_active(rms, th) -> bool`; `class Segmenter(th)` with `.push(frame_idx, active) -> list[Event]` emitting `onset(frame_idx)` and `stop(frame_idx)` live (stop = first frame of an inactivity run that reaches `inactivity_run_ms`, reported with the run's *start* index), and `segment(active: np.ndarray[bool]) -> list[(onset, end)]` for post-run use; both share one implementation (`_scan`). Tests `test_activity.py`: silence → RMS = −inf → inactive; a −40 dBFS tone → active, −50 → inactive; pattern `active×50, inactive×24, active×10` = one utterance (gap 480 ms < 500); `active×50, inactive×25, active×10` = two utterances with stop at frame 50; live `Segmenter` and batch `segment` give identical boundaries on 1,000 random patterns (seeded). Done when: passes and `Segmenter` has no dependency on `onnxruntime`.

**M1-T09 — Playout.** File: `playout.py`. Spec §1.3. Signatures: `Playout(clock, store, wav, th, segmenter)`; `.enqueue(frame_pcm, t_recv_ns)`; `.flush(t_ns, reason)` (idempotent; logs `flushed=1` on the next played row and an event); `.run()` (accounted task; starts playing when ≥ 3 frames queued or on first underrun after start; plays one frame per `frame_ms` on the clock; underrun → silence frame, logged; writes right channel, RMS, `queue_depth`, `backlog_ms = queue_depth * frame_ms`); `.backlog_ms_at(t_ns)` (binary search over logged rows); `.subscribe(cb)` for onset/stop. Tests `test_playout.py` (VirtualClock, fake store): 3 frames enqueued at t=0 → first play at t=0 (depth 3); 1 frame at t=0 → nothing until the 3rd arrives; 50 frames enqueued instantly at t=0 → `backlog_ms_at(t=0)` = 940 (47 frames waiting after the first plays) and the last plays at 980 ms; `flush` at t=200 ms with 40 queued → next row `flushed=1`, depth 0, the ear WAV has silence from 200 ms; two flushes at the same t → one event; a cancel registered at the same `t_ns` as a frame play applies first (frame not played). Done when: passes.

**M1-T10 — Recorder.** File: `recorder.py`. Signatures: `Recorder(path, rate)`; `.write_left(frame_pcm, t_ns)`, `.write_right(frame_pcm, t_ns)` (streams to a stereo WAV via `soundfile.SoundFile`, pads the other channel with zeros to keep alignment by frame index); `.close() -> sha256 of the file`. Tests `test_recorder.py`: 100 left + 100 right frames → 2 channels × 16,000 samples; left only → right is zeros; closing twice is safe; the sha256 equals `hashlib` of the bytes. Done when: passes.

**M1-T11 — Event-anchored latencies.** File: `metrics/latency.py`. Spec §1.5 (names only; audio-anchored ones computed here too but on `CallRecord`). Signatures: `t_ttfa_recv(rec)`, `t_stop_model(rec)`, `t_tool_offset(rec)`, `t_turn_gap_ear(rec)`, `t_stop_ear(rec)`, `t_respond_ear(rec)`, `t_dead_air(rec)`, each `-> list[Observation]`; one `observe(rec, th)` calling all. Tests `test_latency.py` with hand-built `CallRecord`s: caller turn end at 10.000 s, first audio byte at 10.420 → `t_ttfa_recv = 420`; first ear onset at 10.500 → `t_turn_gap_ear = 500`; onset at 9.800 before the caller's turn onset 9.000? no: an agent utterance that began at 8.500 (before the caller onset 9.000) and ends 9.300 is excluded, next onset 10.500 → 500; overlay onset 20.000, agent active until 20.300 then inactive ≥ 500 ms → `t_stop_ear = 300`; no inactivity run within 2.0 s → `valid=False, tag not_stopped`; overlay end 21.000, next onset 21.350 → `t_respond_ear = 350`; cancel event 20.080 → `t_stop_model = 80`, `playout_penalty = 220`; a call with gaps 2.5 s and 1.9 s and a 4 s gap during a pending tool → `t_dead_air = 2.5`, `dead_air_in_tool_s = 4.0`, max gap 2.5. Done when: passes; every function docstring cites §1.5.

**M1-T12 — Mock agent adapter.** File: `adapters/mock_agent.py`. Signatures: `MockAgent(clock, program: MockProgram)` where `MockProgram(first_audio_ms=400, stop_after_interrupt_ms=250, tool_at_ms=1200, reply_len_ms=3000, behaviour: Literal["stops","never_stops","stops_on_everything","short_reply","cancel_restart","perfect","stale_writer","claims_success","silent_in_tool","filler_then_result"])`, emitting a 440 Hz tone at −20 dBFS as speech, events per §2.3, driven only by `clock`. It "hears" the caller via an energy detector on the frames it receives (−45 dBFS), so overlays trigger its programmed stop. Tests `test_mock_agent.py` (VirtualClock): with `stops`, a caller frame burst at t=5.000 s produces `cancel_or_truncate` at 5.250 and no audio after; `never_stops` keeps emitting; `tool_at_ms` yields a `tool_call` at 1.200 s after the turn end. Done when: passes.

**M1-T13 — Silero cross-check (offline only).** File: `metrics/silero_check.py`; add `onnxruntime` and the pinned Silero ONNX file hash to `docs/licenses.md`. Signatures: `boundaries(wav_right, rate) -> list[(onset_ms, end_ms)]`; `disagreement_ms(rms_segments, silero_segments) -> float` (max absolute boundary difference over matched segments). Tests `test_silero_check.py`: on `tests/fixtures/tone_segments.wav` (three tone bursts), disagreement < 40 ms; the module is never imported by `playout.py` or `runner.py` (assert via `sys.modules` after importing them). Done when: passes; model file ≤ 2 MB downloaded once with its hash recorded.

**M1-T14 — Calibration.** File: `calibrate.py`, `configs/runs/calibrate.yaml`. Spec §1.4; pattern: asr-bench `report/calibrate.py`. Behaviour: runs 20 mock calls (RealClock) with `MockProgram(400, 250, 1200)` and one scripted interruption at 2.0 s; computes event-anchored errors (`t_ttfa_recv − 400`, `t_stop_model − 250`, `t_tool_offset − 1200`), audio-anchored errors (`t_turn_gap_ear − (400 + resampler_delay)`, `t_stop_ear − 250`), loop-lag p99, pacing-slip p99, Silero disagreement; a reference-ASR stage (D4) is added in M4-T02 and reports "skipped" until then. Pass = `|event error| p95 ≤ 5`, `|audio error| p95 ≤ 20 + 60`, lag p99 < 5, slip p99 < 10, disagreement < 40. Output: a table and `results/calibration-<ts>.json`; exit 2 on fail. Tests `test_calibrate.py` (VirtualClock run of the same program): all errors = 0 ± 1 frame; the JSON has every field. Done when: `uv run duplexbench calibrate` runs on the laptop and prints the table (it may fail the lag gate on Windows; that is a report, not a bug).

**M1-T15 — Runner.** File: `runner.py`. Signatures: §2.7. Depends on T01–T12. Tests `tests/integration/test_runner_mock.py` (VirtualClock, MockAgent, in-memory store, temp WAV): a 10 s scripted call produces a WAV of 10 s ± 1 frame, `events` rows for `session_open`, `first_audio_byte`, `closed`, playout rows = 500 ± 2; an adapter that raises on the 100th frame → outcome `transport_error`, WAV closed and readable, rows flushed, the next call in the same run still runs; `call_timeout_ms` reached → outcome `timeout`; `stall_ms` reached → `stalled`. Done when: passes.

**M1-T16 — Golden M1.** Files: `tests/golden/make_golden_m1.py`, `tests/golden/expected_m1.json` (WAV sha256, event list with `t_ns`, playout row count), `tests/golden/test_golden_m1.py`. Run under VirtualClock, seeds fixed (fwd 11, back 12), C1 condition, MockAgent `stops`. Test: regenerate in a temp dir and compare to expected exactly. Done when: two consecutive regenerations are identical; the test passes.

**STOP M1: show `pytest -q` summary, `calibrate` table, and the golden hash. Wait.**

### M2 — Scenarios, language pack, stimuli, overlays, control twins, Track A metrics, text report (approve TTS stimulus cost first)

**M2-T01 — Scenario schema and loader.** Files: `scenarios/schema.py`, `scenarios/loader.py`. Signatures: §2.5; `load_pack(dir) -> Pack`; `validate(scenario) -> list[str]` (errors: overlay anchor not a step; Y-INT without ≥ 3 words or without `response_anchors`; H-BC token not in the langpack list; P-PAUSE `pause_ms` outside `[600,1500]`; placement `in_pause` on a step without `pause_ms`; E-END without ≥ 6,000 ms silence; private split loaded from inside the repo). Tests `test_scenario_schema.py`: a valid fixture loads; each error above is produced by a one-field mutation; pack hash changes when one byte of a referenced clip changes; `load_pack` refuses a `private-test` scenario whose path is under the repo root. Done when: passes.

**M2-T02 — Language pack.** Files: `stimuli/langpack.py`, `configs/langpacks/en-US/{ycmd_tokens.yaml, hbc_tokens.yaml, success_patterns.yaml, recovery_patterns.yaml, normalizer_rules.yaml}`. Public lists from spec §3.2 (Y-CMD: no, stop, wait, hold on, hang on, one second, sorry?; wait-class: wait, hold on, hang on, one second; H-BC: mm-hm, uh-huh, yeah, right, okay, got it, sure). `LangPack.load(public_dir, private_dir | None) -> LangPack` with `.hash`; private extensions are merged only when `DUPLEX_PRIVATE_DIR` is set. Tests `test_langpack.py`: lists match the spec; hash changes when a private file is present; the public hash is stable. Done when: passes.

**M2-T03 — Stimulus importer.** File: `stimuli/importer.py`. Signatures: `import_clip(path, out_dir) -> ClipMeta(onset_ms, end_ms, duration_ms, rate, sha256)`; trims leading/trailing silence at −50 dBFS with 50 ms padding, stores `speech_onset_ms`, `speech_end_ms`, writes a 24 kHz mono WAV. Tests `test_importer.py`: a fixture with 300 ms leading silence → `onset_ms = 50 ± 5`; the clip length after trim is the speech length + 100 ms ± 5. Done when: passes.

**M2-T04 — TTS stimulus generation (approve-first).** File: `stimuli/tts.py`, `cli stimuli generate`. Vendor: the one recorded in `docs/providers.md` for simulator TTS with word timestamps (not Dograh's TTS, else disclose). Signatures: `synthesize(text, voice, rate) -> (pcm, rate, words: list[Word(text,start_ms,end_ms)])`; results cached by sha256 of `(vendor, voice, rate, text)`. Prints projected cost (`chars × price`) and waits for `--yes`. Tests `test_tts.py`: with a fake vendor client, caching works and words are stored; the real client is never constructed in tests. Done when: the public-dev clips for the 20 public scenarios exist after one approved run; cost printed and written to `docs/worklog.md`.

**M2-T05 — Overlay engine.** File: `stimuli/overlay_engine.py`. Spec §3.2–3.3. Signatures: `OverlayEngine(clock, scenario, playout, caller, store, th)`; on `onset` from playout for the agent turn that follows `anchor_step`, schedules injection at `onset + offset_ms`; at injection time, if the agent utterance has already ended (`stop` seen) → tag `not_injected`, no audio; else `caller.inject(clip, level_db)` and record `scheduled_ns`, `actual_ns`; `in_pause` overlays are scheduled from the caller step's pause start; after the run, tag `contested` if `|actual − scheduled| > contested_offset_ms`, if an H-BC falls within 1 s after a reference-ASR text ending in "?", if a pause > `contested_pause_ms`, or a one-word Y-INT outside the Y-CMD list. Tests `test_overlay_engine.py` (VirtualClock, MockAgent): `short_reply` (ends at 1.2 s) with offset 2.0 s → `not_injected`; `stops` with offset 0.5 s → injected at onset + 500 ms ± 1 frame; an injection delayed by 350 ms (simulated by a slow caller) → `contested`; `in_pause` fires at the pause start + 0 ms. Done when: passes.

**M2-T06 — Scripted caller source.** File: `stimuli/scripted_caller.py`. Signatures: `ScriptedCaller(scenario, clips, clock)` implementing `CallerSource`: `frames()` yields 20 ms frames for each step in order with the step's scripted silences, pauses and resumptions; `inject(clip, level_db)` mixes an overlay into the outgoing stream from the next frame (overlay voices are summed into the caller channel at the stated level); `turn_ends()` yields `(step_id, t_ns)`. Tests `test_scripted_caller.py`: two steps of 1.0 s with 6 s silence after the last → 400 frames + `turn_end` events at 1.0 s and 8.0 s; an injected 0.4 s clip at −6 dB appears in the mix at the right frames with the right RMS. Done when: passes.

**M2-T07 — Control twins and validity.** File: `scenarios/control_twin.py`. Spec §3.4. Signatures: `make_twin(scenario) -> Scenario` (no overlays, same steps, `is_control=True`); `align(overlay_rec, control_rec) -> dict[step_id, (agent_turn_overlay, agent_turn_control) | None]` by caller step; `validity(overlay_row, control_turn, metric, budget_ms, th) -> tag | None` returning `ambiguous_end` when the control's matched turn lasted < `offset + window` (window = budget for Yield@B; 2,000 for `t_stop_ear` and Hold), `unmatched_control` when alignment is `None`. Tests `test_control_twin.py`: control turn 1.2 s, offset 0.5 → valid at B=0.5, `ambiguous_end` at B=1.0 and 2.0; step sequence `[s1,s2,s3]` vs `[s1,s3]` → s2 `unmatched_control`; Track C pass rates pair at scenario level without alignment. Done when: passes.

**M2-T08 — Floor-control metrics.** File: `metrics/floor_control.py`. Spec §4.1. Signatures: `observe(rec, th) -> list[Observation]` producing `yield_rate` (per budget, tag `budget=<B>`), `talk_over_share`, `wait_compliance`, `hold_speaking_rate` (2,000 and 1,000 windows), `content_leakage_rate` (needs reference-ASR text; `valid=False` tag `no_refasr` until M4), `hold_listening_rate`, `response_rate` (2 s and 5 s), with validity from T07. A yield counts only when a `stop` (ear inactivity) begins within B; provider cancel events never count. Tests `test_floor_control.py` — the CLAUDE.md M2 table, built from MockAgent programs under VirtualClock through the runner: (a) `stops` 300 ms → Yield@1.0 = 1.0, Hold-speaking = 1.0; (b) `never_stops` → 0.0 / 1.0; (c) `stops_on_everything` → 1.0 / 0.0; (d) `short_reply` → `ambiguous_end` at 2.0 s offsets (excluded) and `not_injected` where applicable; (e) `cancel_restart` (cancel then restart 20 ms later, no 500 ms gap) → Yield 0.0, Hold 1.0; contested items appear only with the `contested` tag; a P-PAUSE with no onset before resumption → Hold-listening 1.0; an E-END with onset at 1.3 s → response_rate@2 = 1, `t_turn_gap_ear = 1300`. Done when: all cases pass.

**M2-T09 — TOR diagnostic.** File: `metrics/diagnostics.py`. Uses the exact rule copied in M0-T03 (file:line quoted in the docstring). Tests `test_diagnostics.py`: the three examples in the FDB code comments (or, if none, three hand cases: silence → not takeover; a 0.8 s one-word response → not takeover; a 1.5 s three-word response → takeover). Done when: passes.

**M2-T10 — Respond/Resume classifier.** File: `classifiers/respond_resume.py`. Spec §4.1. Signatures: `label(utt_text, anchors, prior_utt_text, th) -> Literal["respond","resume","uncertain"]`; content-word Jaccard after the normalizer. Tests `test_respond_resume.py`: anchor present → respond; Jaccard 0.6 and no anchor → resume; Jaccard 0.3 and no anchor → uncertain; empty text → uncertain. Every observation it produces carries tag `classifier`. Done when: passes.

**M2-T11 — Text report (early).** File: `report/text.py`. Signatures: `render(store, run_id) -> str` — a fixed-width table of every metric in `names.py` present in `observations` with `n`, value, CI placeholder "(CI in M7)", tags counts, and the caveat lines for `ambiguous_end`, `not_injected`, `contested`. Tests `test_text_report.py`: on the M1 golden store + M2 observations, the output contains each metric name once and the counts match the fixture. Done when: passes.

**M2-T12 — Score command and golden M2.** Files: `cli.py` (`score <run_id>`: loads each call into `CallRecord`, runs every `observe`, writes `observations`), `tests/golden/make_golden_m2.py`, `expected_m2.json` (the Track A table for the 5-call mock run with overlays and twins), `test_golden_m2.py`. Done when: regeneration is identical twice; the test passes.

**STOP M2: show the 5-call mock smoke text report and the golden diff (0). Wait.**

### M2b — Human inputs gate (no code beyond tools)

**M2b-T01 — Scenario template generator and validator.** `cli scenario new --domain --track --split` writes a YAML with every field and a comment per field; `cli scenario validate <dir>` runs `validate` from M2-T01 and prints a table. Tests: generated template validates after the required fields are filled by a fixture. **M2b-T02 — `cli preregister`.** Hashes: every `configs/systems/*.yaml` config_id, pack hash, langpack hash, thresholds hash, private tree hash (from `DUPLEX_PRIVATE_DIR`), harness git commit; writes `results/preregister-<ts>.json`; `cli run` refuses when any hash differs from the latest record. Tests: changing one threshold after preregister → refusal. **M2b-T03 — `cli label`.** Terminal tool: plays a clip (`report/clips.py` cut, see M7-T04), shows reference-ASR text, records `label` and `labeler_id` to `results/labels/<set>.jsonl`, resumable, two labelers per item; sets: `respond_resume`, `success_claim`, `recovery_request`, 200 items each. Tests: resume after 10 items continues at item 11; the output has both labelers per item.

**STOP M2b: print the list of missing human inputs (40 private + 20 public scenarios, private extensions, pre-registration, label sessions) and wait.**

### M3 — First live adapter: gpt-realtime over live stream (approve ≈ $5)

**M3-T01 — Prices and cost projection.** Files: `configs/prices/openai_realtime.yaml` (`url`, `checked`, `unit`, `usd_per_unit`, `notes`), `metrics/cost.py`. Signatures: `project_call_usd(system_cfg, scenario, prices) -> float` (audio minutes in + out at the priced rates, plus the simulator line when present); `actual_call_usd(usage_event, prices) -> float | None` (None → the row shows `usd: null` and the run stops before the next call). Tests `test_cost.py`: a 3-minute Track A call with fixture prices projects the hand-computed value; a missing price → `None`. Done when: passes; the price URL and date are real.

**M3-T02 — Budget.** File: `budget.py` (§2.10). Tests `test_budget.py`: `max_usd=10`, reserve 4, reserve 4, reserve 4 → third raises; settle the first at 2 → a reserve of 4 now succeeds. Done when: passes.

**M3-T03 — OpenAI Realtime adapter.** File: `adapters/openai_realtime.py`; fixtures `tests/fixtures/openai_realtime/*.jsonl` (recorded once with `--yes`, secrets stripped). Behaviour: WebSocket session per call; `session.update` with the formats and turn detection from `configs/systems/gpt-realtime.yaml`; frames sent as `input_audio_buffer.append` (μ-law when `native_mulaw`); audio deltas → `audio` events; the cancel signal mapped per `docs/providers.md` (candidate: `input_audio_buffer.speech_started` with `interrupt_response: true`, confirmed by `response.done{status: cancelled}`); the reference-client flush: on that signal the harness flushes playout and sends `conversation.item.truncate` with the played `audio_end_ms` — record this choice in `docs/providers.md` under "reference client behaviour"; `function_call_arguments.done` → `tool_call`; `tool_result` → `conversation.item.create` + `response.create`; `usage` from `response.done`. Tests `test_openai_realtime.py`: replay each fixture through the adapter with a fake socket and assert the harness event sequence and timings; a socket close mid-stream yields `error` then `closed`, never an exception. Done when: passes on fixtures; the live 5-call smoke (next task) is separate.

**M3-T04 — Live stream transport and run command.** Files: `transport/live_stream.py` (the adapter-over-WebSocket pacing wrapper with pacing-slip logging), `cli run <run.yaml> [--yes]`. `run.yaml`: systems, scenarios (ids or split), conditions, trials, concurrency, `max_usd`. The command prints: calibration status (must be fresh ≤ 24 h on this host), the preregister check (skipped for `smoke`), projected cost per system, and waits for `--yes`. Tests `test_cli_run.py`: without `--yes` nothing is sent (fake adapter counts opens = 0); with `--yes` and a budget below the projection → refusal before the first call. Done when: passes.

**M3-T05 — Smoke (≈ $5, approve-first).** `configs/runs/smoke.yaml`: 5 Track A public-dev scripted calls, C0, gpt-realtime. Run; then `score`, `report text`. Accept: every Track A metric row populated (or tagged with a reason), calibration passed, actual cost printed; one call with the provider socket killed by the harness at 30 s (`--chaos transport_drop`) → `transport_error`, recording present, run continues. Done when: the gate items are shown.

**STOP M3: show the text report, the chaos result, and the actual cost. Wait.**

### M4a — Reference-ASR candidate table ($0; downloads ≤ 2 GB each need `--yes`)

**M4a-T01 — Engine interface and fake.** File: `refasr/engine.py`. Signatures: `class Word(text, start_ms, end_ms, conf)`; `class RefASR(Protocol)`: `name`, `version`, `transcribe(pcm8k) -> list[Word]` (batch), `stream() -> Streamer` with `push(frame) -> list[Word]` (partials, may be empty) and `finish() -> list[Word]`; `FakeASR(script: list[Word])` for tests. Tests `test_refasr_engine.py`: the fake returns its script; a streamer yields partials in order. Done when: passes.

**M4a-T02 — Candidate table.** Files: `refasr/candidates/<name>.py` (one wrapper per candidate), `docs/refasr-candidates.md`. Candidates (confirm availability and license first; do not assume): faster-whisper `large-v3-turbo` (batch, word timestamps), NVIDIA Parakeet-TDT 0.6B v3 and Nemotron streaming (NeMo), Kyutai STT (streaming), moonshine. For each: license, size, streaming support, word timestamps, WER on `tests/fixtures/floor-mini/` (20 TTS read-backs through C0 and C1, produced in M2-T04), RTF on the laptop CPU, partial latency p90 in streaming mode where supported. Output: the table. **Human picks the model (never-decide #3).** Done when: the table has every column for every reachable candidate; blocked ones say why.

**STOP M4a: show the table; wait for the model choice. Then pin it in `refasr/pinned.py` (name, version, file hashes) and add its package.**

### M4 — Mock tools, Track C, reference ASR, simulator, golden M4 (approve simulator cost first)

**M4-T01 — Pinned reference ASR + calibration stage (D4).** Files: `refasr/pinned.py`, `calibrate.py` (add the stage). The stage streams `tests/fixtures/calib_speech_60s.wav` (a checked-in synthetic or public-domain clip, no TTS spend) through the pinned engine; records `refasr_rtf`, `refasr_partial_p90_ms`, `refasr_version` in the calibration JSON; `cli run` refuses any run with a simulator when either exceeds `Thresholds.refasr_*`. Tests `test_calibrate_refasr.py` with `FakeASR` timed on the VirtualClock: partial p90 350 ms → refusal for a Track C run config; a Track A config proceeds. Done when: passes.

**M4-T02 — Domain backends.** Files: `tools/mock_backends/{scheduling,orders,billing,account,outbound}.py`, `tools/state.py`. Signatures per backend: `class Backend`: `tools() -> list[ToolSchema]` (JSON schema per tool, `kind` read/write), `call(name, args) -> dict` (raises `PolicyError` for a forbidden write; records `verify_identity` success), `state() -> dict` (canonical: sorted keys, ISO dates, normalized phone/email), `snapshot_hash() -> str`, `reset(seed_state)`; results include the `result_anchors` the scenario declares (e.g. the slot time string). `state.canonicalize(state) -> dict` and `expected_equals(state, expected) -> bool`. Tests `test_backends.py`: each backend passes a scripted happy path to the expected state; a write before `verify_identity` on `account` raises `PolicyError`; canonicalization makes `"(555) 123-4567"` equal `"5551234567"`; hash is stable across key order. Done when: passes for all five.

**M4-T03 — Latency and failure injector.** File: `tools/latency_injector.py`. Signatures: `Injector(clock, tooldef)` `.apply(call) -> result` sleeping `latency_ms` on the clock, then `timeout` (no result; the tool call row gets `injected_failure=timeout` and result_ns = start + latency), `error` (HTTP 500 body), `partial` (result with one required field missing). Tests `test_injector.py` (VirtualClock): the four modes produce the documented rows. Done when: passes.

**M4-T04 — Tool server and client.** Files: `tools/server.py`, `tools/client.py` (§2.8). Tests `tests/integration/test_tool_server.py`: start the app with `uvicorn` in-process on a free port; a POST through `httpx` and a `ToolClient.call` produce identical `tool_calls` rows and state; an unknown `call_token` → 404; the state endpoint returns the canonical state. Done when: passes.

**M4-T05 — Task metrics.** File: `metrics/task.py`. Spec §4.2 + D6. Signatures: `observe(rec, th)` producing `pass_at_1` (state equals expected AND every `required_spoken` found in the reference-ASR text before the matching write AND no `PolicyError`), `pass_pow_3` (scenario-level, computed in `stats/`), `stale_action_rate`, `preemptive_tool_rate` (`t_tool_offset < 0` on writes), `policy_violation_rate`, `auth_bypass_rate`, `tool_wait_pass`, `filler_count`, `filler_s`, `dead_air_in_tool_s`, `repeated_prompt_rate`, `response_rate_per_turn`, `call_outcome`, `t_tool_to_info_ear` (utterance onset rule) with the diagnostic `anchor_offset_in_utt_ms` from reference-ASR word times on the ear channel, `unscripted_overlap` tagging. Tests `test_task.py` via MockAgent programs under VirtualClock through the runner with the tool server: `perfect` → `pass_at_1 = 1.0` on every public-dev scenario fixture; `stale_writer` (writes the pre-correction value) → `stale_action_rate = 1.0` on correction scenarios; `claims_success` (says "all set" with no write) → `unsupported_claim_rate = 1.0` tagged `classifier`; `silent_in_tool` (5 s silence during a 5 s tool wait) → `dead_air_in_tool_s = 5.0`, and no simulator speech; `filler_then_result` → `t_tool_to_info_ear` = the post-result delay, not ~0; a utterance "one moment your slot is three pm" with word times → `anchor_offset_in_utt_ms = 700`. Done when: all pass.

**M4-T06 — Success-claim classifier.** File: `classifiers/success_claim.py`. Pattern set from the langpack (+ private extension when mounted); `claims(text) -> list[Span]`; an unsupported claim = a claim with no successful matching write in the call before the claim's utterance end. Tests `test_success_claim.py`: "I've booked that for you" → claim; "I can book that" → no claim; a claim after a successful write → supported. Tag `classifier`. Done when: passes.

**M4-T07 — Simulator turn policy.** File: `simulator/turn_policy.py`. Spec §7. Signatures: `TurnPolicy(clock, th, rng_seed)` state machine: `AGENT_SPEAKING → (300 ms inactivity) SPECULATING → (700 ms) FLOOR_WAIT(U(300,800)) → SPEAKING`; `tool_pending(True)` blocks floor-taking until `tool_pending(False)` + `sim_patience_ms`; `on_agent_active()` from any state before `SPEAKING` discards speculation and returns to `AGENT_SPEAKING`; while `SPEAKING`, agent activity → emit `unscripted_overlap` and finish the utterance. Outputs: `next_action() -> Speak(text_ready_required) | Wait`; records `think_ms` and `speculative` per turn. Tests `test_turn_policy.py` (VirtualClock): agent stops at t=0 → first speech between 1,000 and 1,500 ms; a resumption at 400 ms cancels it; tool pending from 0–5,000 → no speech before 6,500; `think_ms` logged when the reply is late. Done when: passes.

**M4-T08 — Caller LLM.** Files: `simulator/caller_llm.py`, `simulator/prompts/caller_v1.md` (hashed). `anthropic` SDK, model from `configs/simulator.yaml` (default `claude-sonnet-5-5`, temperature 0). Signatures: `CallerLLM(client, prompt_path).next_utterance(ctx: SimContext) -> str | END`; `ctx` carries persona, goal, facts, the step script with the scripted self-correction, the transcript so far (agent side = reference-ASR text, never provider text), `tool_pending`. Tests `test_caller_llm.py`: with a fake client, the prompt contains the goal entities and the pending flag; `END` is returned when the fake says so; the prompt hash is stable. No live call in tests. Done when: passes.

**M4-T09 — Caller TTS and simulated caller source.** Files: `simulator/caller_tts.py` (reuses `stimuli/tts.py`), `simulator/sim_caller.py` implementing `CallerSource`: speculative synthesis at `SPECULATING`, discarded if the agent resumes; word timestamps logged to `sim_turns`; `T_caller_think` = agent turn end → caller audio onset. Tests `test_sim_caller.py` (VirtualClock, fake LLM/TTS): the sequence of `sim_turns` rows for a 3-turn script with one discarded speculation. Done when: passes.

**M4-T10 — Streaming reference ASR in the loop.** File: `simulator/asr_stream.py`: feeds ear frames to `pinned.stream()`, exposes the running text to the LLM context, and when the adapter emits `output_transcript`, computes `mishearing_rate` (WER between provider transcript and reference text per agent turn). Tests `test_asr_stream.py` with `FakeASR`: partial text accumulates; mishearing on a scripted mismatch = the hand-computed WER. Done when: passes.

**M4-T11 — Validation gate and regeneration.** File: `simulator/validation_gate.py`. Spec §7 + §1 row 10: after a trial, check (on the caller's own text) that goal entities were stated within two caller turns, audio not broken (no all-silence TTS), no harness timeout; failure → the whole trial is rerun as a new call with `regenerated=<n>`; max `sim_regen_max`; the third failure → outcome `validation_failed`; all attempts kept and costed; `regeneration_rate` per scenario; > `sim_regen_review_rate` → the scenario is listed for human review before any paid run. Tests `test_validation_gate.py`: a fake caller that omits the entity twice then states it → 2 regenerations, 3 call rows, the last scored; three failures → `validation_failed`. Done when: passes.

**M4-T12 — Golden M4.** `make_golden_m4.py`: 3 public-dev Track C scenarios × 1 trial with `MockAgent(perfect)`, fake LLM (scripted replies), fake TTS (tone + synthetic word times), `FakeASR`, VirtualClock → `expected_m4.json` = task table + sim_turns summary. `test_golden_m4.py`. Done when: identical on regeneration.

**M4-T13 — Simulator smoke (approve-first, cents).** 3 public-dev scenarios × 1 trial, `MockAgent(perfect)` as the agent, real LLM + real TTS + pinned ASR: regeneration rate ≤ 10 %, `T_caller_think` p90 ≤ 1.5 s on the instance (report if on the laptop), goal entities stated within two turns. This is the LLM eval for the prompt; record the prompt hash and result in `docs/worklog.md`. Done when: shown at the gate.

**STOP M4: show the task table from the golden, the simulator smoke numbers and cost. Wait.**

### M5 — Track D entities and the reference-ASR floor ($0 build; approve floor-set TTS)

**M5-T01 — Normalizer port + extensions.** Files: `metrics/normalize.py` (copy of asr-bench with attribution), `tests/unit/test_normalize.py` (copied unchanged — CRITICAL regression contract; do not edit expectations), then extensions in the same file: NATO alphabet (`alpha bravo` → `ab`), spelled letters, email tokens (`at`/`dot`), ZIP, DOB, dollar amounts; new cases appended in `test_normalize_ext.py`. Tests: the copied tests pass unchanged first; then `"j as in juliet o h n"` → `john`; `"five five five one two three four"` → `5551234`; `"john dot doe at example dot com"` → `john.doe@example.com`; `"march third nineteen ninety"` → `1990-03-03`; `"forty two dollars and ten cents"` → `42.10`. Done when: both files pass.

**M5-T02 — Entity metrics.** File: `metrics/entities.py`. Spec §4.3. `capture_first` (earlier of first tool call carrying the slot and first read-back in the reference-ASR text), `capture_final`, `readback_presence`, `readback_error_rate` (exact for most types; CER for IDs and phones), `codec_penalty` (W − C0 paired). Tests `test_entities.py`: a mock that reads back a wrong phone then writes the right one → first wrong, final correct; a write with no read-back → `readback_presence = 0`; CER of `AB12X` vs `AB12K` = 0.2. Done when: passes.

**M5-T03 — Recovery-request classifier.** File: `classifiers/recovery_request.py`; patterns from the langpack; `recovery_request_rate` and `recovery_success` after a wrong first attempt; tag `classifier`. Tests: "could you spell that" → request; "thanks" → none. Done when: passes.

**M5-T04 — Floor calibration (approve TTS cost first).** File: `refasr/floor_calibration.py`, `cli floor`. Generates ≥ 200 read-back items (≥ 25 per type) and the `required_spoken` confirmations with known text, plays them through C0 (and C1 when asked) and the pinned ASR, stores per-type floors (error rate with Wilson CI) and the confirmation floor in `results/floor-<ts>.json`; the run card embeds the latest; entity results within the floor are flagged `within_floor`. Tests `test_floor.py` with `FakeASR` that corrupts 10 % of digits: the floor for phones = 0.10 ± Wilson CI; the flag fires on a result of 0.08. Done when: passes; the real floor run is approved separately.

**STOP M5: show the floor table. Wait.**

### M6 — Remaining adapters, one at a time (approve ≈ $5 each)

Order: **M6-T01** Pipecat cascade baseline (`adapters/pipecat_cascade.py`; components and endpointing parameters from `configs/systems/cascade.yaml`; barge-in cancels LLM and TTS; its interruption event → `cancel_or_truncate`); **M6-T02** Dograh stack (`adapters/dograh_stack.py`: harness-as-carrier per `docs/dograh-ingress.md`: create the run, open the telephony WS, send `connected` and `start`, stream `media` frames, receive `media` → `audio`, `clear` → `cancel_or_truncate`, `mark` → logged; tool calls arrive through `tools/server.py` keyed by `call_token` carried in `customParameters`; if M0-T05 said FALLBACK B, build the in-process pipeline with `components_only=True` in `caps` and every table marks it); **M6-T03** Gemini Live; **M6-T04** Grok Voice; **M6-T05** GPT-Live-1; **M6-T06** self-hosted full-duplex (optional; GPU approval). Each: `docs/providers.md` confirmed first; recorded-event fixtures; `tests/unit/test_<adapter>.py` replaying fixtures incl. a mid-stream close; `transport/twilio_media.py` (M6-T02 prerequisite: `encode_*`/`decode_*` for `connected/start/media/mark/clear/stop`, tested on the message shapes from Twilio's docs); a 5-call C0 smoke with cost printed. If a provider has no cancel event, `caps.cancel_event=False` and nothing is faked. **STOP after each adapter.**

### M7 — Statistics, planner, report, verify, golden M7 ($0)

**M7-T01 — Wilson and bootstrap.** Files: `stats/wilson.py`, `stats/paired_bootstrap.py` (copy asr-bench `metrics/stats.py` `ci`/`paired_diff`, attribution). Signatures: `wilson(k, n, z=1.96) -> (lo, hi)`; `bootstrap_ci(values, clusters, stat, resamples, seed)`; `paired_diff(pairs: list[(a, b, cluster)], stat, resamples, seed) -> (diff, lo, hi)`; `holm(pvalues) -> list[bool]`. Pair key per §1 row 12. Tests `test_stats.py`: `wilson(50,100) = (0.4038, 0.5962) ± 0.0005`; `wilson(0,10) = (0.0, 0.2775) ± 0.0005`; `paired_diff` on identical sides → diff 0 with CI containing 0; on a constant +0.2 difference → CI excludes 0; asymmetric drop: a pair with one invalid side is dropped and counted; `holm([0.01,0.04,0.03])` → `[True, False, False]` at α 0.05. Done when: passes.

**M7-T02 — Planner.** File: `stats/planner.py`, `cli planner`. Signatures: `half_width(n, p=0.5) -> float` (Wilson); `pairs_needed(delta, discordant, power=0.8, alpha=0.05) -> int` with `n = ((z_a·√d + z_b·√(d − δ²))² / δ²)`; `percentile_min_n(p) -> int` (30/100/200/1000). Tests `test_planner.py`: `half_width(50/100/200/400)` within ±0.3 pts of 13.6/9.7/6.9/4.9; `pairs_needed(0.10, 0.30) = 233 ± 5`; `pairs_needed(0.20, 0.40) = 76 ± 5`; `percentile_min_n(99) = 1000`. The CLI prints the spec §9.2 table and refuses "distinguishable" wording for a design below the needed n. Done when: passes.

**M7-T03 — Aggregation.** File: `report/aggregate.py`: `aggregate(store, run_id) -> dict` with, per metric × system × condition: `n`, value, CI (Wilson for rates; bootstrap clustered by scenario for percentiles; p99 only when n ≥ 1,000 else `"insufficient n"`), completion rate beside percentiles, tag counts, `classifier`/`judged`/`diagnostic` markers, `self_hosted` and `components_only` flags; paired differences for the pre-registered comparisons (≤ 6, Holm); `pass_pow_3` by scenario. Rejects any metric name outside `names.py`. Runs with differing pack/langpack/normalizer/refasr/simulator hashes never aggregate (raises). Tests `test_aggregate.py`: on a fixture store with two systems: counts, CIs and the paired difference equal hand values; a mixed-hash fixture raises. Done when: passes.

**M7-T04 — Clips, caveats, HTML.** Files: `report/clips.py` (`cut(wav, t0, t1, events) -> (clip_path, timeline_json)`, used by `cli label` too), `report/caveats.py` (every spec §10 item 10 condition → a caveat line; fixture-driven), `report/html.py` (Jinja2; run card; profile table; the two floor-control charts as inline SVG; latency tails with model-level twin and playout penalty; task table with `anchor_offset_in_utt_ms`; entity table with floors; frontier charts with the dashed guides; condition deltas; failure clip browser; `--public` omits clips and private scenario text; no composite anywhere). Tests `test_report.py`: every spec §10 item renders on the golden fixture (assert anchor strings); `--public` output contains no `<audio>` and no private scenario id; caveats fire on a fixture with low n, `ambiguous_end`, `not_injected`, `unmatched_control`, `slow_caller`, `unscripted_overlap`, an unvalidated classifier, a missing cancel event. Done when: passes.

**M7-T05 — Verify and netcheck.** Files: `report/verify.py` (`verify(store, run_id, report_dir) -> list[str]` recomputes every number from the store and diffs the rendered JSON; `[]` = clean), `cli netcheck` (20 TCP connect / TLS / light round-trip medians per endpoint → run card). Tests: verify on the golden = `[]`; a tampered JSON → one diff line. Done when: passes.

**M7-T06 — Run card and golden M7.** `report/run_card.py` assembles spec §10 item 1 (hashes, thresholds, calibration incl. RTF, floors, simulator patience rule, iteration counts, vendors that received audio, instance type, netcheck). `make_golden_m7.py` renders the M4 golden run end to end; `expected_m7/` holds the JSON and `index.html` sha256; `test_golden_m7.py`: byte-identical re-render, `verify == []`. Done when: identical twice.

**STOP M7: show the golden HTML report and `verify` output. Wait.**

### M7b — Classifier validation (human labels; $0)

**M7b-T01 — `classifiers/validate.py`**: `kappa(labels_a, labels_b)`, `human_agreement`, `pattern_recall(classifier_output, human_labels)`, `flip_rate` (for future LLM classifiers; 5 reruns); writes the three results to the run card; a classifier below `kappa_min` keeps the `classifier` marker and stays out of headline rows. Tests: kappa on a textbook 2×2 table = the known value; perfect agreement = 1.0; recall on a fixture = hand value. Then run the three `cli label` sessions with the two Dograh labelers. **STOP: show the validation table.**

### M8 — Core run (approve projected cost; spec estimate $300–700 per system)

**M8-T01** `cli preregister` both configs and all thresholds. **M8-T02** `cli run core.yaml --yes` on the whole private pack (mounted from `DUPLEX_PRIVATE_DIR`), c = 1, Dograh stack + one reference S2S. **M8-T03** `score`, `report`, `verify`. Accept (§1 row 9): calibration passed on the instance; harness-invalid calls ≤ `harness_invalid_max`; every attempt preserved; `ambiguous_end` / `not_injected` / `unmatched_control` / `contested` rates reported per system; pre-registered coverage n met for every headline metric (planner notes printed); run card complete. **STOP: present the report; wait for the decision on Full.**

### M9 — Phone path and concurrency (approve trunk and run cost; Q5 decides the trunk)

**M9-T01** `transport/phone_path.py`: two numbers on the trunk; caller leg plays stimulus and records received audio (the ear); agent leg receives, bridges to the adapter through `twilio_media.py` (or the Telnyx equivalent), uses `mark` for playback confirmation and `clear` on cancel. **M9-T02** `transport/loopback_baseline.py`: caller leg → harness echo endpoint; trunk round trip and jitter medians stored before each phone run. **M9-T03** the 10-scenario C0 subset; report raw and baseline-subtracted latencies, both labeled; per-metric sign agreement of paired differences vs live stream. **M9-T04** `load/scheduler.py` + `load/sysmon.py` (copies; attribution; keep their tests), `configs/runs/load_c8.yaml`: open-loop arrivals, ramp–sustain–cool-down, c = 1 and c = 8 on Track A; error / 429 / disconnect rates and latency shift per level. Tests: scheduler tests copied unchanged; sign-agreement table on a fixture. **STOP.**

### M10 — Full run (approve projected cost; spec estimate $4k–8k)

Only after Core has run at least twice and been reviewed. `configs/runs/full.yaml` per spec §9.3; `--public` report for publication; outside review before any public claim (spec §2.3).

## 4. Golden fixtures and regeneration

| Golden | Made by | Contents frozen | Regenerate when |
|---|---|---|---|
| M1 | `make_golden_m1.py` | WAV sha256, event list, playout row count | playout, channel, clock or recorder changes |
| M2 | `make_golden_m2.py` | Track A observation table incl. tags | any Track A metric, overlay engine or twin logic changes |
| M4 | `make_golden_m4.py` | task table, sim_turns summary | any Track C metric, tool server or simulator policy changes |
| M7 | `make_golden_m7.py` | aggregate JSON, `index.html` sha256 | stats, aggregation or report changes |

Rule: a golden is regenerated only in the same commit as the intentional scoring change, with the diff of `expected_*.json` described in `docs/worklog.md` and a changelog line (spec §11).

## 5. Gate checklist (what to show at every STOP)

1. `uv run pytest -q` summary line (all green).
2. The milestone's acceptance items, each with the actual number or output beside it.
3. Golden status: unchanged, or regenerated with the reason.
4. Money: projected and actual USD for the milestone, or `$0`.
5. Open items: anything from §6 of CLAUDE.md that came up, with the question written out.
6. `docs/worklog.md` tail.

## 6. Appendix — facts the executor must confirm, not assume

- **Twilio Media Streams** (docs: twilio.com/docs/voice/media-streams/websocket-messages): inbound `connected`, `start{streamSid, callSid, mediaFormat{encoding:"audio/x-mulaw", sampleRate:8000, channels:1}, customParameters}`, `media{payload: base64 μ-law, timestamp, chunk}`, `stop`; outbound `media{streamSid, media{payload}}`, `mark{streamSid, mark{name}}` (echoed back when played), `clear{streamSid}` (empties the buffer; pending marks returned). 20 ms chunking is a convention reported by third parties; confirm on the docs page and record.
- **OpenAI Realtime**: `g711_ulaw` accepted; `output_audio_buffer.clear` WebRTC-only; the WebSocket reference client flushes locally and sends `conversation.item.truncate`. Confirm event names on the current reference page before M3-T03.
- **Gilbert-Elliott defaults** (p = 0.0034, r = 0.1667, loss-in-bad = 1.0 → 2 % average loss, mean burst 6 frames = 120 ms) are derived, [Proposed]; replace with tau2-bench's values if M0-T03 finds them, and record the source.
- **Reference ASR**: nothing is pinned until the M4a table and the human choice; Deepgram is never a candidate.
- **Simulator TTS vendor**: must offer word timestamps; must not be a cascade-under-test vendor, or the overlap is disclosed in the run card.
- **Dograh run creation**: unknown until M0-T05; the fallback is explicit and flagged.
