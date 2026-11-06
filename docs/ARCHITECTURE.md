# Architecture — Dograh Duplex Bench harness

State of the code on 2026-11-05. Sources: `docs/BUILD_PLAN.md`, `docs/SPEC.md`, the module
docstrings under `src/duplexbench/`, and the decision records in `docs/decisions.md`.
Paths in this document are relative to `src/duplexbench/` unless they start with `docs/`,
`configs/` or `tests/`.

## 1. Purpose

The benchmark decides which voice-agent architecture Dograh ships for each type of US
English phone call. It also decides whether a change to Dograh's own stack made calls
better or worse. The harness plays a caller into a system under test over an 8 kHz
telephone channel and scores what the caller hears. It scores four tracks, floor control,
task under conversation, entity capture, and latency with load and cost, and never one
composite score. Every number carries its sample size and a confidence interval, for
Dograh's engineers at the release gate and for the public reading the redacted report.

## 2. Layer diagram

Each box names every module of its layer. Arrows show the direction of control between
layers. The three columns in the live-call layer are the caller side, the channel and the
agent side. The layers below the live-call layer run after the call, except the time and
recording layer, which the live call uses on every frame.

```
+------------------------------------------------------------------------------------------+
| CLI (Typer, one command per function)                                                    |
|   cli.py (doctor, loads the cli_* modules)   cli_run.py (run)   cli_calibrate.py         |
|   cli_score.py (score)   cli_report.py (report, verify, planner, netcheck)               |
|   cli_load.py (load, card-fill, gate)   cli_tools.py (preregister, label, scenario new   |
|     and validate, report-text)   cli_stimuli.py (stimuli generate)                       |
|   cli_refasr.py (refasr-bench)                                                           |
+------------------------------------------------------------------------------------------+
                                            |
                                            v
+------------------------------------------------------------------------------------------+
| Orchestration (one run = many calls; one call = runner.run_call)                         |
|   runner.py   context.py   budget.py   systems.py   preregister.py   calibrate.py        |
|   gates.py   netcheck.py   run_card_fill.py   labeling.py   overlays/scripted_run.py     |
|   load/runner.py   load/scheduler.py   load/sysmon.py                                    |
+------------------------------------------------------------------------------------------+
          |                               |                               |
          v                               v                               v
+----------------------------+  +----------------------+  +--------------------------------+
| Caller side                |  | Channel              |  | Agent side                     |
|   stimuli/caller_source.py |  |   channel/chain.py   |  |   adapters/base.py             |
|   stimuli/scripted_caller  |  |   channel/mulaw.py   |  |   adapters/mock_agent.py       |
|     .py                    |->|   channel/resample   |->|   adapters/openai_realtime.py  |
|   stimuli/importer.py      |  |     .py              |  |   adapters/ws_adapter.py       |
|   stimuli/tts.py           |  |   channel/noise.py   |  |   adapters/_convert.py         |
|   stimuli/fake_tts.py      |<-|   channel/bursts.py  |<-|   adapters/gemini_live.py      |
|   stimuli/langpack.py      |  |   channel/muffle.py  |  |   adapters/grok_voice.py       |
|   scenarios/schema.py      |  |   channel/           |  |   adapters/gpt_live.py         |
|   scenarios/loader.py      |  |     gilbert_elliott  |  |   adapters/pipecat_cascade.py  |
|   scenarios/control_twin   |  |     .py              |  |   adapters/dograh_stack.py     |
|     .py                    |  |                      |  |   adapters/selfhosted_fd.py    |
|   scenarios/template.py    |  | forward: caller ->   |  |   transport/live_stream.py     |
|   overlays/engine.py       |  |   agent              |  |   transport/twilio_media.py    |
|   simulator/               |  | back: agent ->       |  |   transport/phone_path.py      |
|     simulated_caller.py    |  |   caller (playout)   |  |   transport/                   |
|   simulator/turn_policy.py |  |                      |  |     loopback_baseline.py       |
|   simulator/caller_llm.py  |  |                      |  |                                |
|   simulator/audio.py       |  |                      |  |                                |
|   simulator/               |  |                      |  |                                |
|     validation_gate.py     |  |                      |  |                                |
|   simulator/prompts/       |  |                      |  |                                |
+----------------------------+  +----------------------+  +--------------------------------+
          ^                                                                |
          | tool start / result (subscribe)                                | tool_call
          |                                                                v
+------------------------------------------------------------------------------------------+
| Tools (one code path for the HTTP route and the in-process client)                       |
|   tools/server.py   tools/client.py   tools/state.py   tools/latency_injector.py         |
|   tools/mock_backends/scheduling.py   tools/mock_backends/orders.py                      |
|   tools/mock_backends/billing.py   tools/mock_backends/account.py                        |
|   tools/mock_backends/outbound.py                                                        |
+------------------------------------------------------------------------------------------+
          ^                                   ^                                   ^
          | every frame, every event          | every frame, every event          |
+------------------------------------------------------------------------------------------+
| Time and recording (used by every layer above)                                           |
|   clock.py (Clock, RealClock, VirtualClock)   _loop.py (precise selector loop)           |
|   frames.py   playout.py   recorder.py   storage.py (Store, SQLite)   thresholds.py      |
+------------------------------------------------------------------------------------------+
                                            |
                                            | events.sqlite + <call_id>.wav, after the call
                                            v
+------------------------------------------------------------------------------------------+
| Scoring (cli score; reads the store and the WAV, writes the observations table)          |
|   metrics/names.py   metrics/base.py   metrics/activity.py   metrics/latency.py          |
|   metrics/floor_control.py   metrics/task.py   metrics/entities.py   metrics/cost.py     |
|   metrics/diagnostics.py   metrics/normalize.py   metrics/silero_check.py                |
|   classifiers/respond_resume.py   classifiers/success_claim.py                           |
|   classifiers/recovery_request.py                                                        |
|   refasr/engine.py   refasr/floor_calibration.py   refasr/candidates_table.py            |
|   refasr/candidates/faster_whisper.py   refasr/candidates/kyutai.py                      |
|   refasr/candidates/moonshine.py   refasr/candidates/parakeet.py                         |
+------------------------------------------------------------------------------------------+
                                            |
                                            | observations
                                            v
+------------------------------------------------------------------------------------------+
| Statistics and report (cli report, verify, planner, gate)                                |
|   stats/wilson.py   stats/paired_bootstrap.py   stats/planner.py                         |
|   report/aggregate.py   report/text.py   report/html.py   report/clips.py                |
|   report/caveats.py   report/verify.py   report/run_card.py                              |
+------------------------------------------------------------------------------------------+
```

Notes on the diagram:

- The channel column has two directions. `ChannelChain.forward` carries caller audio to
  the agent. `ChannelChain.back` carries agent audio to the playout queue.
- The tools layer has two entry points. A hosted adapter calls `ToolClient.call`. A Dograh
  workflow sends `POST /tools/{token}/{tool}` to the FastAPI app. Both paths call
  `ToolServer.invoke`, so the `tool_calls` rows are identical.
- The scoring layer never runs inside the live loop. The spec (§1.1) requires this.
- `thresholds.py` holds every numeric threshold. A test refuses a bare threshold literal in
  any other module.

## 3. Live-call diagram

This diagram shows one call inside `runner.run_call`. The runner spawns four accounted
tasks with `clock.task`. The names in the diagram are the task names in `runner.py`.
A fifth small task, `_turns`, maps caller turn ends to send times. It is not an audio
path, so the diagram does not show it.

Arrow key: `-->` data or a call in the direction of the arrow. `..>` a notification
(subscribe callback or event). Every component writes rows with `Store.put`, which the
diagram shows once at the bottom.

```
                 Clock (RealClock or VirtualClock; clock.task, sleep_until_ns, idle)
                 every task below is spawned with clock.task; every wait is a clock wait
 ............................................................................................
 : CALLER SIDE                                      AGENT SIDE                              :
 :                                                                                          :
 : ScriptedCaller (Track A, D)                      Adapter (system under test)             :
 : or SimulatedCaller (Track C)                       MockAgent | OpenAIRealtimeAdapter |   :
 :       |                                            GeminiLive | GrokVoice | GptLive |    :
 :       | frames()  (20 ms Frame, turn ends)         PipecatCascade | DograhStack | ...    :
 :       v                                                 ^                  |             :
 : +----------------+   ChannelChain.forward         send_frame           safe_events()     :
 : | _send_caller   |--------------------------------->|                    |               :
 : |  paces frames  |   (resample, mu-law, noise,      |                    v               :
 : |  on the clock  |    bursts, muffle, GE loss)   +--------------------------------+      :
 : +----------------+                               | _recv_agent                    |      :
 :       |      |                                   |  audio -> ChannelChain.back    |      :
 :       |      | ctx.turn_ended(t)                 |        -> Playout.enqueue      |      :
 :       |      +---------------------------------->|  cancel_or_truncate            |      :
 :       |        (adapter emits the next           |        -> Playout.flush(t)     |      :
 :       |         first_audio_byte)                |  tool_call -> ToolClient.call  |      :
 :       |                                          |        -> adapter.tool_result  |      :
 :       | Recorder.write_left                      +--------------------------------+      :
 :       v   (caller audio as delivered)                   |                 |              :
 : +-------------------------------+                       | enqueue /       | tool_call    :
 : | Recorder                      |                       | flush           v              :
 : |  left  = caller, right = ear  |<-- write_right        |      +--------------------+    :
 : |  <call_id>.wav                |    (ear audio)        |      | ToolServer         |    :
 : +-------------------------------+         |             v      |  invoke:           |    :
 :                                           |   +---------------+ |   Injector         |   :
 :                                           +---| _play         | |   (latency and     |   :
 :                                               | Playout.run   | |    failure, on     |   :
 :       caller.inject(clip, level_db)           |  jitter       | |    the clock)      |   :
 :       ^                                       |  buffer,      | |   Backend.call     |   :
 :       |                                       |  one frame    | |   (mock database,  |   :
 : +----------------+   onset / stop (subscribe) |  per tick,    | |    policy)         |   :
 : | _overlays      |<............................  RMS,         | +--------------------+   :
 : | OverlayEngine  |                             |  Segmenter    |    ^          :         :
 : |  .run          |<-- ctx turn ends            +---------------+    |          :         :
 : |  schedules     |                                   :              |          :         :
 : |  injections,   |                                   :     POST /tools/{token}/{tool}    :
 : |  tags          |                                   :     (Dograh workflow or Pipecat   :
 : |  not_injected  |                                   :      cascade, over HTTP)          :
 : +----------------+                                   :              |          :         :
 :                                                      :              |          : start / :
 : TRACK C ONLY (SimulatedCaller replaces               :              |          : result  :
 : ScriptedCaller; no OverlayEngine on a Track C call)  : onset /      |          :         :
 :                                                      : stop         |          :         :
 : +------------------------------------------------+   :              |          :         :
 : | SimulatedCaller                                |<..+              |          :         :
 : |                                                |<.................+..........+         :
 : |  TurnPolicy (state machine on the clock)       |   ToolServer.subscribe(token, cb)     :
 : |   AGENT_SPEAKING -> SPECULATING -> FLOOR_WAIT  |                                       :
 : |   -> SPEAKING; the floor is blocked while a    |                                       :
 : |   tool is pending and for sim_patience_ms      |                                       :
 : |   after its result                             |                                       :
 : |        |                                       |                                       :
 : |        | next_action() == Speak                |                                       :
 : |        v                                       |                                       :
 : |  CallerLLM.next_utterance(ctx) ----------------+--> Anthropic Messages API             :
 : |        |                       <---------------+--- (network wait, awaited directly)   :
 : |        | text, or END (hang up)                |                                       :
 : |        v                                       |                                       :
 : |  CallerVoice (TTS -> 8 kHz frames) -> frames() |   on_agent_text(text) <- AgentTextTap :
 : +------------------------------------------------+   (output_transcript events)          :
 :                                                                                          :
 : SUPERVISOR (the run_call coroutine, on the 20 ms frame grid):                            :
 :   adapter closed | caller script exhausted | E-END silence -> completed                  :
 :   call_timeout_ms -> timeout     stall_ms -> stalled     exception -> transport_error    :
 :   budget.reserve before adapter.open; BudgetExceeded -> budget_refused, no call          :
 :..........................................................................................:
                                            |
                                            | Store.put(table, row) from every component:
                                            | events, playout, overlays, tool_calls,
                                            | sim_turns, calls
                                            v
                              +---------------------------------+
                              | Store (storage.py)              |
                              |  queue -> writer thread ->      |
                              |  SQLite WAL, batches of 100     |
                              |  await store.flush() at the end |
                              +---------------------------------+
```

Notes on the diagram:

- `_send_caller` pulls frames from the caller source and sends each one through
  `ChannelChain.forward` to `adapter.send_frame`. It writes the delivered frame to the left
  WAV channel and logs caller turn onsets, turn ends and overlay onsets at send time.
- `_recv_agent` iterates `adapter.safe_events`. Audio goes through `ChannelChain.back` into
  `Playout.enqueue`. A `cancel_or_truncate` event calls `Playout.flush`. A `tool_call`
  event calls `ToolClient.call`, then the runner returns the result with
  `adapter.tool_result`.
- `_play` is `Playout.run`. It plays one frame per tick on the clock and writes the right WAV
  channel. It writes one `playout` row per tick and reports onsets and stops to subscribers.
- `_overlays` is `OverlayEngine.run`. It subscribes to playout onsets and stops and reads
  the caller turn ends from the context. It injects overlay clips with `caller.inject`. When
  the agent utterance ends before the scheduled offset, it tags the overlay `not_injected`.
- On a Track C call the `SimulatedCaller` is the caller source and there is no overlay
  engine. Its `TurnPolicy` receives agent onsets and stops from `Playout` and tool start
  and result notifications from `ToolServer.subscribe`. When the policy returns `Speak`,
  the caller asks `CallerLLM` for the next utterance, synthesizes it with `CallerVoice`,
  and queues the frames for `frames()`.
- The `CallerLLM` request is a network wait. The caller awaits it directly, not through
  `clock.idle`, so virtual time holds still during the request.
- A Dograh workflow or a Pipecat cascade does not send `tool_call` on the socket. It sends
  `POST /tools/{token}/{tool}` to the `ToolServer`. The adapter learns of the call through
  `ToolServer.subscribe` and logs it. The runner does not invoke the tool a second time.
- The supervisor checks the end conditions once per frame. On every exit it cancels the
  tasks, closes the adapter, closes the WAV, writes the `calls` row, and awaits
  `store.flush()`.
