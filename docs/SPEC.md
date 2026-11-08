# Dograh Duplex Bench — Specification v2 (final, v1 scope)

Date: 2026-11-04 (revision 2, after independent review). Replaces `FULL_DUPLEX_BENCHMARK_SPEC.md`. Labels: **[Fact]** evidence base or attached file; **[Inference]** reasoning; **[Assumption]** unverified, must be checked; **[Proposed]** a threshold or choice introduced here with its justification. Vendor numbers are claims, never facts. Every numeric threshold in this document is **[Proposed]** unless labeled otherwise; all of them are configuration with these defaults, pre-registered before a run, never changed per system.

---

## 0. What the benchmark decides

One harness answers two questions for US English phone agents: *which architecture ships for which call type*, and *did a change to Dograh's stack make calls better or worse*. Four architectures are compared on equal terms: native full-duplex models, turn-based speech-to-speech models, a tuned streaming cascade, and Dograh's production stack.

| Track | Question it answers | Headline metrics (verifiable; no LLM judge) |
|---|---|---|
| **A. Floor control** | Does it stop when it should, keep going when it should, and start when it should? | Yield rate, Hold-while-speaking rate, Hold-while-listening rate, Response rate, `T_stop_ear`, `T_turn_gap_ear` |
| **C. Task under conversation** | Does it do the job correctly while being interrupted, corrected and kept waiting by tools? | Pass@1, Pass^3, Stale-action rate, Pre-emptive tool-call rate, Policy-violation rate. Unsupported-success-claim rate joins the headline only after its classifier is validated (§8) |
| **D. Entities over the phone** | Does it capture and read back names, numbers and IDs through an 8 kHz line? | Capture accuracy per type, Recovery success, Read-back entity error rate |
| **L. Latency and cost** | What does the caller wait, and what does a minute cost? | p50/p90/p95 of the **ear-level** latencies (`T_turn_gap_ear`, `T_stop_ear`, `T_respond_ear`, `T_tool_to_info_ear`, `T_dead_air`), cost per connected minute, error rate at concurrency |

Every scenario belongs to one track and logs every metric. Tracks B (listener behavior), E (long-call memory), G (output naturalness) and H (jailbreaks, proactive safety) from the draft are deferred (§12): none changes the architecture decision in v1 and each needs a judge or human reference data.

**Scope.** US English, G.711 μ-law 8 kHz default channel, hosted and self-hosted systems, live streaming as the primary transport, a verification subset over a real SIP trunk. No Hindi, Hinglish or India work. The language layer (TTS voices, overlay token lists, pattern sets, normalizers) is a pluggable pack; only `en-US` is built.

---

## 1. Measurement foundations

### 1.1 One clock, two channels, one event log

- The harness owns one monotonic clock (`time.perf_counter_ns()` behind a `Clock` abstraction; `monotonic_ns` has 15.6 ms resolution on Windows [Fact, ASR harness clock.py]), as the ASR harness does [Fact, ASR harness v2]. Every audio frame sent or received, adapter event and tool call is stamped on it. The clock is an abstraction with two implementations: **real** (live runs) and **virtual** (tests and golden replays: deterministic time, fixed channel seeds in both directions), so a mock run reproduces byte-for-byte.
- Every run produces a **stereo recording** at 8 kHz, 16-bit: left = caller audio as delivered to the system under test (after the channel model, decoded); right = agent audio **at the caller's ear** (§1.3). The wideband diagnostic condition records at 16 kHz.
- The **event log** holds: stimulus events (overlay id, intent class, scheduled and actual send time, clip speech onset and end); simulator utterance events (text, TTS word timestamps, send time); adapter events (session open, first audio byte received, provider cancel/truncate, provider output transcript if any, errors, reconnects); tool events (call start, arguments, result time, injected latency, injected failure); playout events (frame played, queue depth, flush); network samples; harness health (event-loop lag, pacing slip).
- Metrics are computed **after the run** from the recording, the event log and the mock database. Nothing is scored inside the live loop.

### 1.2 Ground-truth anchors

- **Caller-side anchors come from what the harness synthesized, never from ASR.** Scripted clips are trimmed at import and store `speech_onset_ms` and `speech_end_ms`. Simulator utterances are TTS output with word timestamps; the harness knows the text and the time each word is played. Caller turn end = send time of the clip's last speech frame.
- **Agent-side activity at the ear is read from the frames the harness played.** A played 20 ms frame is *active* if its RMS exceeds −45 dBFS (typical telephone noise floors sit well below; confirmed on the trunk loopback). An *inactivity run* is ≥ 500 ms of inactive frames (longer than inter-word gaps, shorter than a deliberate stop; the draft's 300 ms fires on sentence boundaries). An **agent utterance** is a maximal sequence of active frames not interrupted by an inactivity run. This replaces VAD on the agent channel, so there is no VAD onset bias. A pinned Silero VAD run offline is a cross-check only.
- **Agent-side model events** (first audio byte, cancel/truncate, tool call) come from the adapter log. Each adapter documents which provider event maps to each harness event; the coding agent confirms names against current docs.

### 1.3 Reference playout model

What the caller hears depends on how the client plays provider audio. The harness fixes this so it is a constant across systems and makes its consequences visible:

- Provider audio is resampled to 8 kHz, μ-law encoded, packetized at 20 ms, passed through the return network model (§5), decoded, and placed in a **playout queue**. Playout starts when 60 ms (three frames) is queued, the smallest buffer that survives 2% Gilbert-Elliott loss without underrun in pre-tests (the coding agent verifies and records the value). Frames play at real-time pace on the harness clock.
- Audio that arrives faster than real time waits in the queue. **Queue depth is logged every frame**; `backlog_ms` at each overlay onset is a per-system diagnostic. Turn-based speech-to-speech APIs typically build a backlog of seconds; frame-synchronous full-duplex models hold roughly the jitter buffer [Inference; measured and reported, not assumed].
- On a provider cancel/truncate event, or the cascade's own interruption event, the queue is **flushed immediately**. For a system with no such event the harness invents nothing: the ear hears the backlog, which is what a real caller would hear from a client that cannot know to flush. The gap `T_stop_ear − T_stop_model` is reported as the **playout penalty** so readers can see how much of a stop latency is backlog rather than model.
- The same playout model runs on the agent leg of the phone path (§6.2); there the ear is the caller leg's received audio, which adds the trunk.

### 1.4 Harness calibration

A mock agent with known behavior runs before every published result and on every new instance (reuses the ASR harness `calibrate` pattern [Fact]):

- Event-anchored timings (`T_ttfa_recv`, `T_stop_model`, `T_tool_offset`) within ±5 ms of the mock's programmed values.
- Audio-anchored timings (`T_turn_gap_ear`, `T_stop_ear`) within ±1 frame (20 ms) plus the 60 ms buffer of programmed values [Inference: nothing finer exists at 20 ms framing].
- Event-loop lag p99 < 5 ms and pacing slip p99 < 10 ms, else the run is invalid [Fact: ASR harness thresholds].
- RMS activity vs the Silero cross-check: segment boundary disagreement > 40 ms on the mock recording fails calibration.
- Reference-ASR stage [Proposed, eng review D4 2026-11-04]: a 60 s checked-in clip streamed through the pinned reference ASR; any run with a simulated caller is refused when TTFS p90 > 700 ms or real-time factor > 0.5, so a slow instance is caught before spend rather than through `slow_caller` exclusions after it. TTFS (Time To Final Segment, pipecat stt-benchmark definition) = final transcript receipt minus speech end, the segmenter hold included; 700 ms is `sim_eot_ms`, so a passing final lands before the caller's end of turn. *(Corrected 2026-11-08, B22: the earlier "partial-latency p90 > 300 ms" gated per-word interim latency, which no streaming ASR meets and nothing in the harness waits for.)*

### 1.5 Latency taxonomy (the only names allowed)

Anchors: **caller turn end** (last speech frame sent); **overlay onset / end** (first / last speech frame of an injected overlay); **agent onset** (first active frame at the ear of an agent utterance); **first agent byte received**; **provider cancel event**; **tool start / result**; **stop** (start of the first inactivity run after an overlay onset).

| Name | From | To | Notes |
|---|---|---|---|
| `T_turn_gap_ear` | caller turn end | agent onset of the first agent utterance that **begins after the caller's turn onset** | signed; negative = agent started while the caller was still speaking. Tail audio from an agent utterance that began before the caller started is excluded |
| `T_ttfa_recv` | caller turn end | first agent audio byte received | model-level diagnostic |
| `T_stop_ear` | overlay onset | stop | defined only when the stop begins within 2.0 s; otherwise "not stopped" |
| `T_stop_model` | overlay onset | provider cancel/truncate or cascade interruption event | diagnostic; absent for systems without such an event |
| `T_respond_ear` | overlay **end** | agent onset of the first agent utterance that begins after the stop | anchored at overlay end so a long interruption does not inflate it; undefined if no stop. Recording-only; the Respond/Resume label (§4.1) is applied to that same utterance afterwards |
| `T_tool_offset` | end of the caller turn carrying the last required argument | tool call start | signed; negative = pre-emptive |
| `T_tool_to_info_ear` | tool result returned | agent onset of the first agent utterance whose reference-ASR text contains a **result anchor** (declared by the mock tool result, e.g. the slot time) | key-information delivery [Fact: FDB v3 reports this breakdown]; filler before the information does not shorten it |
| `T_dead_air` | per call | sum of gaps ≥ 2 s with neither channel active after the first caller turn, excluding gaps during a pending tool call; the maximum single gap and the dead air **during** tool waits are reported separately | seconds |
| `T_caller_think` | agent turn end (ear inactivity ≥ 700 ms) | caller audio onset | simulator property; logged for validity; never a system metric |

The phrase "barge-in latency" never appears in a report. No other latency names are permitted.

---

## 2. Systems under test

### 2.1 Architectures and what each adapter must record

| Architecture | Examples (names from the handoff; existence and access confirmed by the coding agent) | Adapter records |
|---|---|---|
| Native full-duplex | GPT-Live-1; Moshi / PersonaPlex self-hosted | backend model, reasoning effort, audio format negotiated, cancel-event support |
| Turn-based speech-to-speech | gpt-realtime, Gemini Live, Grok Voice | turn-detection type and parameters, interrupt setting, input/output formats |
| Streaming cascade | Pipecat or LiveKit pipeline: streaming STT + end-of-turn model + LLM + streaming TTS | each component and version, endpointing parameters, barge-in policy, filler policy, LLM sampling |
| Dograh stack | Dograh production configuration (Pipecat-based cascade, Deepgram STT, litellm-routed LLM [Fact, project memory]) | same as cascade plus the production config hash and workflow id |

Self-hosted systems are flagged in every table; their latency includes the instance rather than a vendor network path [Inference].

### 2.2 The cascade baseline is defined, not assumed

"Tuned" means: streaming STT with interim results, a dedicated end-of-turn model or tuned endpointing (not a fixed silence timeout alone), streaming TTS with first-chunk playout, barge-in that cancels LLM and TTS on caller speech, and a configuration selected on the **public-dev split** under the same iteration budget as Dograh's stack (§2.3). A batch Whisper → LLM → TTS pipeline is not a permitted baseline [Fact: draft principle 6; published cascade baselines are untuned [3P]].

### 2.3 Fairness rules (Dograh's conflict of interest)

- **Pre-registration.** Before any private-split run, each system's full configuration, the scenario-pack hash, the overlay offsets and every threshold in this document are hashed and committed with a timestamp. Private-split results are reported only for pre-registered hashes. Nothing is changed per system after seeing results.
- **Tuning budget.** All tuning happens on the public-dev split; iterations per system are logged and disclosed. Dograh's stack and the cascade baseline get the same budget (10 iterations per release cycle).
- **Whole-pack runs.** The Core tier runs the entire private-test pack; no subset selection exists to game.
- **External task set.** Optional import of τ-bench domains [Fact: public in tau2-bench] gives tasks Dograh did not write.
- **Disclosure.** Published results state who tuned each system, iteration counts, and which vendors received audio.
- **Outside review.** No public claim is made before a reviewer outside Dograh has read the methodology and the run card [Fact: the Decision Record's conflict-of-interest mitigation].

---

## 3. Stimuli and ground truth

### 3.1 Scenario

A versioned YAML file plus audio assets: caller source (scripted TTS clips or simulator persona), ordered **caller steps**, overlay events with intent classes anchored to a caller step and an offset, tool definitions with injected latency and failures, expected end state, required spoken confirmations, entity references, result anchors, conditions and trial count. Scenarios are split `public-dev` or `private-test`; the pack has a content hash stored with every run.

### 3.2 Overlay taxonomy: label by construction

Every overlay carries an **intent class**. The class defines correct behavior; no word count or duration threshold is used as a label. Overlays are rendered with TTS voices distinct from the main caller voice where the class implies a different speaker, at the stated level offsets. Overlays scheduled "during agent speech" are triggered live by **agent onset** (§1.5) plus the offset; if the agent's utterance ends before the scheduled offset, the overlay is **not injected** and the event is tagged `not_injected` (counted, reported).

| Class | Content (closed lists in the language pack; a held-out private extension exists, §11) | Injected | Correct behavior | Scored as |
|---|---|---|---|---|
| **Y-INT** content interruption | correction, objection or new question, ≥ 3 words, authored with **response anchors** (words a relevant reply must contain) | during agent speech at offset 0.5 s or 2.0 s after agent onset (0.3 s in the draft falls inside playout start-up) | stop, then respond to the new content | Yield, `T_stop_ear`, Respond/Resume, `T_respond_ear` |
| **Y-CMD** short command | "no", "stop", "wait", "hold on", "hang on", "one second", "sorry?" | during agent speech | stop; wait-class items ("wait", "hold on", "hang on", "one second") also require silence until the scripted resumption 3–6 s later, with no idle prompt | Yield, Wait-compliance |
| **H-BC** backchannel | "mm-hm", "uh-huh", "yeah", "right", "okay", "got it", "sure", each ≤ 1 s | during agent speech at 0.5 s or 2.0 s | keep speaking; no content response | Hold-while-speaking |
| **H-SIDE** side speech | 2–6 words addressed to a third party ("can you grab the mail"), −6 dB | (a) during agent speech; (b) inside a caller mid-turn pause (§ P-PAUSE geometry) | (a) keep speaking; (b) do not start | Hold-while-speaking; Hold-while-listening |
| **H-BG** background speech | other-room speech bed with intelligible words, −12 dB | (a) and (b) | same | same |
| **H-TIC** vocal tic | cough, sneeze, laugh, throat clear | (a) and (b) | same | same |
| **P-PAUSE** thinking pause | 0.6–1.5 s silence at a syntactically incomplete point in a caller turn, followed by a scripted completion ("my callback number is … five five five …"); below the ~2 s most server VADs treat as end of turn, above inter-phrase gaps | inside a caller turn | do not start before the resumption | Hold-while-listening |
| **E-END** end of turn | a complete caller turn followed by ≥ 6 s of scripted silence | — | respond | Response rate, `T_turn_gap_ear` |

All "(b)" injections sit **inside a caller turn**, in a mid-turn pause followed by a scripted resumption, never in the post-turn gap, so a correct agent is silent by construction and Response rate on E-END is unaffected.

**[Inference]** "Hold on" is a command to the agent, not side speech, so it is Y-CMD. "No!" is Y-CMD whatever its duration. This resolves the draft's contradiction with the FDB takeover rule, kept only as a diagnostic (§4.5).

### 3.3 Contested bucket

Items whose correct behavior reasonable people dispute are tagged `contested` post hoc and reported in their own table, never in a headline: any H-BC token injected when the agent's reference-ASR text in the 1 s before injection ends in a question; pauses longer than 1.5 s; one-word interruptions outside the Y-CMD list; overlays whose actual injection offset (from the recording) deviates more than 300 ms from schedule.

### 3.4 Matched control trial and event validity

Every scenario with overlays has a **control twin**: same scenario, same caller audio or simulator script, no overlays. Overlays are injected live without knowledge of the control; validity is decided after the run.

- **Alignment.** Agent turns are matched between overlay and control runs by **caller step** (the scripted step the agent is replying to), not by raw turn index. In Track A (scripted callers) the two runs have identical caller steps. In Track C the simulator follows the same step script; where the two conversations diverge (step count or order differs), the event is tagged `unmatched_control` and excluded from Yield and Hold while still counting for task metrics.
- **Validity window per metric.** An overlay event is valid for a metric only if the control's matched agent turn continued at least `offset + window`: the stop budget B for Yield@B (so the validity set differs per budget and is reported per budget), 2.0 s for `T_stop_ear` and Hold-while-speaking. Otherwise it is tagged `ambiguous_end` for that metric [Inference: without this, short-reply systems get free yields].
- **Reporting.** `ambiguous_end`, `not_injected` and `unmatched_control` rates are reported per system. Offsets are pre-registered and never adjusted per system; a high `ambiguous_end` rate is a finding about reply length, not a reason to change the instrument.
- The control twin also supplies the interrupted-vs-clean split in Track C at the scenario level (pass rates), which does not need turn alignment.

### 3.5 Caller sources in v1

- **Scripted TTS callers** for Tracks A and D: at least 6 voices spanning gender and apparent age, two speaking rates. Ground truth is exact; annotation cost is zero [Assumption (a) from the handoff].
- **Simulated caller** (LLM + TTS, §7) for Track C, with scripted overlays layered on.
- **Real consented recordings** arrive in v1.1 with triple-annotated turn ends (Fleiss' kappa reported; TurnBench reached 0.78 [Fact]). The scenario format already carries `source: recorded` and `speaker_id`.

---

## 4. Tracks and metrics

Each metric lists definition, source, computation, sample-size requirement. n is per system per condition unless stated; arithmetic in §9.

### 4.1 Track A — Floor control

| Metric | Definition | Source | Computation | n |
|---|---|---|---|---|
| **Yield rate @B** | share of events valid at B (Y-INT and Y-CMD) where a stop begins within B of overlay onset | recording | B ∈ {0.5, **1.0**, 2.0} s; headline B = 1.0 s (τ-Voice's 2 s window [Fact] is lenient for a correction; 0.5 s is near the floor of human reaction; all three reported). Provider cancel events do **not** count as a stop; only ear inactivity does, so cancel-and-restart earns nothing | ≥ 200 valid pooled for ±7 pts; ≥ 100 per class in Full |
| **`T_stop_ear`** | §1.5 | recording | on events valid at 2.0 s that stopped; p50/p90/p95; "not stopped" share beside it | ≥ 100 for p90 |
| **Talk-over share** | on should-yield events, seconds of agent active frames between overlay onset and overlay end divided by overlay duration | recording | mean and p90 | same events |
| **Respond / Resume / Uncertain** | label of the agent utterance that defines `T_respond_ear`: Respond if its reference-ASR text contains ≥ 1 response anchor; Resume if content-word Jaccard overlap with the reference-ASR text of the utterance in progress at overlay onset is ≥ 0.5 and no anchor; else Uncertain | recording, reference ASR, scenario anchors | rule-based; validated on 200 double-labeled items at kappa ≥ 0.7 before the rate is published; labeled `classifier` until then | ≥ 100 Y-INT events |
| **`T_respond_ear`** | §1.5 | recording | p50/p90 over all stopped Y-INT events (label-independent) | ≥ 100 |
| **Wait-compliance** | share of wait-class Y-CMD events with no agent active frame between overlay end and the scripted resumption | recording | — | ≥ 50 |
| **Hold-while-speaking rate** | share of events valid at 2.0 s (H-BC, H-SIDE(a), H-BG(a), H-TIC(a)) where no stop begins within 2.0 s of overlay onset (1.0 s also reported; τ-Voice uses 1 s and 2 s selectivity windows [Fact]) | recording | per class and pooled | ≥ 100 pooled in Core; per class in Full |
| **Content-leakage rate** | share of should-hold events where an overlay anchor word appears in the agent's reference-ASR text within 10 s | reference ASR | diagnostic beside Hold | same events |
| **Hold-while-listening rate** | share of P-PAUSE, H-SIDE(b), H-BG(b), H-TIC(b) events with no agent onset between overlay onset and the caller's scripted resumption | recording | 1 − premature-start rate; per class | ≥ 100 pooled in Core; per class in Full |
| **Response rate @W** | share of E-END events with an agent onset within W of caller turn end, W ∈ {2, **5**} s [Fact: τ-Voice reports response rate and yield within 2 s] | recording | always printed beside Hold-while-listening, so a slow or silent agent cannot score well on Hold alone | ≥ 100 |
| **`T_turn_gap_ear`** | §1.5 | recording | full signed distribution on E-END events; p50/p90/p95; share negative | ≥ 100 for p90 |

**Primary chart.** Yield @1.0 s (y) against pooled Hold-while-speaking (x), one marker per system per condition, marker size = Response rate @5 s. Second panel: Hold-while-listening against Response rate. No composite.

### 4.2 Track C — Task under conversation

**Domains** [Assumption (b)]: appointment scheduling, orders and returns, billing and payments, account and identity changes, outbound confirmation calls; 8 private scenarios each (40) and 4 public each (20). Each domain has a mock backend with deterministic state, read and write tools, result anchors, and a policy (e.g. identity must be verified before an account write).

**Scenario features**: injected tool latency 0 / 2 / 5 s and failures (timeout, error, partial) [Fact: τ-Voice tools return instantly, so this is new]; a caller self-correction inside one utterance whose timing is known from TTS word timestamps; a withdrawal or change while a tool is pending; a policy-constrained request; an authentication step; scripted overlays from §3.2.

| Metric | Definition | Source | Computation | n |
|---|---|---|---|---|
| **Pass@1** | final mock-database state equals the expected state after canonicalization, AND every `required_spoken` confirmation is found in the agent's reference-ASR text before the corresponding write (normalized match on the confirmation's entity values and key words; the reference-ASR floor for confirmations is measured in §4.3 and shown beside Pass@1), AND no policy violation | mock DB, tool log, reference ASR | binary per trial; mean over trials | 40 scenarios × 3 trials = 120 trials, ±9 pts; 230 paired trials to detect a 10-point difference (§9) |
| **Pass^3** | all 3 trials pass (k = 3 [Fact: draft default; EVA-Bench's median pass@k − pass^k gap is 0.44]) | same | CI by bootstrap over scenarios | 40 scenarios |
| **Stale-action rate** | among correction/withdrawal scenarios: a write whose arguments contain a value the caller had retracted before that **tool call** started, or a write committed after a withdrawal was spoken | tool log, scenario anchors, TTS timestamps | — | ≥ 20 correction scenarios × 3 trials |
| **Pre-emptive tool-call rate** | share of write tool calls with `T_tool_offset` < 0 [Fact: FDB v3's −2.27 s "Rome" call] | tool log, TTS timestamps | — | all write calls |
| **Unsupported-success-claim rate** | agent speech contains a success claim (pattern set, with the private held-out extension, §11) and no successful matching write exists in the call | reference ASR, tool log | labeled `classifier`; joins the headline after validation (§8); pattern recall on the validation set is reported | all calls |
| **Policy-violation rate** | a write the scenario forbids appears in the tool log; **auth-bypass rate** (writes before a required `verify_identity` success) separately | tool log | — | ≥ 20 policy scenarios × 3 |
| **`anchor_offset_in_utt_ms`** (diagnostic) | reference-ASR word time of the first result-anchor word on the ear recording minus the onset of the utterance that defines `T_tool_to_info_ear`; p50 and the share > 1.0 s reported beside it [eng review D6 2026-11-04: filler and result inside one utterance are the residual the utterance-onset rule cannot see] | ear recording, reference ASR | diagnostic, never headline | same events |
| **Tool-wait behavior** | per latency bucket: Pass@1; filler count and duration (agent active frames between tool start and result); dead air during the wait; repeated-prompt rate (agent utterance whose first 4 content words match an earlier agent utterance) | recording, tool log | by bucket 0 / 2 / 5 s | ≥ 30 trials per bucket |
| **Response rate per caller turn** | share of caller turns followed by an agent onset within 5 s | recording | — | all turns |
| **Call outcome** | completed (agent or caller ends after the goal), caller-ended early, timeout at 8 min, stalled (single gap > 20 s outside a tool wait), transport error | event log | distribution | all calls |
| **Interrupted vs clean split** | every Track C rate for overlay scenarios vs their control twins, at scenario level | — | paired difference with CI | same scenarios |

### 4.3 Track D — Entities over the phone channel

Entity types: spelled person name, email, street address, ZIP code, date of birth, phone number, alphanumeric ID, dollar amount; US formats. Type-specific normalizers extend the ASR harness normalizer (spelled-letter collapse, digit-run merge, NATO alphabet, "at"/"dot") [Fact: that normalizer exists and is ported from Dograh code]. Core covers four priority types (phone, spelled name, alphanumeric ID, date of birth); Full covers all eight.

| Metric | Definition | Source | Computation | n |
|---|---|---|---|---|
| **Capture accuracy (first attempt / final)** | the entity in the tool arguments equals the reference after normalization. **First attempt** = the earlier of the first tool call carrying that slot and the first agent read-back of that slot (so deferring the tool call does not help); **final** = last write before call end | tool log, reference ASR, reference | per type; CER inside IDs and phones secondary | ≥ 50 items per type per condition (±13.6 pts; ±9.7 at 100) |
| **Recovery request rate / Recovery success** | after a wrong first attempt: did the agent ask to spell or repeat (pattern set plus held-out extension), and was the final value correct | reference ASR, tool log | labeled `classifier` until validated | all first-attempt failures |
| **Read-back presence** | the normalized entity appears in the agent's reference-ASR text before the write that uses it | reference ASR, tool log | — | all entity writes |
| **Read-back entity error rate** | normalized entity extracted from the agent's ear-side audio vs the reference; exact match for most types, CER for IDs and phones | ear recording, reference ASR | reported with the **reference-ASR noise floor**: the same metric on a calibration set of TTS-generated read-backs **and `required_spoken` confirmations** with known text through the same channel; results within the floor are flagged indistinguishable | ≥ 50 per type; floor set ≥ 200 items |
| **Codec penalty** | read-back error and capture accuracy under the wideband diagnostic minus phone-clean | both conditions | paired difference | ≥ 100 items |

The **reference ASR** is a pinned open model run on the harness instance (candidate confirmed and pinned by the coding agent; never Deepgram, a Dograh production component [Fact, project memory]). Its version and floor are in every run card.

### 4.4 Track L — Latency, load and cost

| Metric | Definition | Source | Computation | n |
|---|---|---|---|---|
| **Ear-level latencies** | the five headline names in §0; model-level twins in §4.5 | recording, event log | p50 / p90 / p95 with bootstrap CIs; p99 only when n ≥ 1,000, else "insufficient n" [Fact: ASR harness rule] | p50 ≥ 30; p90 ≥ 100; p95 ≥ 200 |
| **Cost per connected minute** | provider charges divided by connected minutes, from usage fields in API responses where exposed, else from the priced config and audio seconds; includes idle session time and re-billed context; voice layer and backend model separate where the provider splits them | adapter usage logs, dated price config | price source URL and check date stored | per call |
| **Concurrency behavior** | c = 1 and c = 8 (the draft's 32 waits for a sized instance), Track A scripted calls, open-loop arrivals, ramp–sustain–cool-down with sustain membership by arrival time [Fact: ASR harness load design] | event log | error / 429 / disconnect rate and latency shift per level | ≥ 100 events per level |
| **Network baseline** | median of 20 TCP connect, TLS handshake and light round trips per endpoint before the run | harness | run card | — |

### 4.5 Diagnostics (reported, never headline)

- **Takeover rate (TOR)** with FDB v1's exact code rule (a response shorter than 1 s with three words or fewer is not a takeover; the rule and thresholds are copied from Full-Duplex-Bench eval_pause_handling.py) [Fact] for cross-paper comparison; thresholds in config. *(Corrected 2026-11-05, B1: the earlier wording "fewer than two words" did not match the code.)*
- `T_ttfa_recv`, `T_stop_model`, `backlog_ms` and the **playout penalty** beside their ear-level twins.
- Simulator mishearing rate, regeneration rate, `slow_caller` and `unscripted_overlap` counts (§7).
- `ambiguous_end`, `not_injected`, `unmatched_control` and contested-bucket results.
- Condition deltas (§5) for every metric with paired CIs.

---

## 5. Channel conditions

**Chain order (caller → agent).** Source audio (TTS at native rate or a recording) → acoustic effects (noise bed, bursts, muffling, level) → band-limit and resample to 8 kHz → μ-law encode → 20 ms packetization → network model (Gilbert-Elliott loss, seeded; lost frames replaced by repeating the previous frame [Assumption: G.711 Appendix I style concealment approximates phone behavior; confirm]) → delivery as μ-law bytes where the provider accepts them, else decode and resample to the provider's rate; delivery path recorded per adapter.

**Return (agent → ear).** Provider audio → resample to 8 kHz → μ-law encode → packetize → network model with an independent seed → decode → playout queue (§1.3) → ear recording. No acoustic effects on the return path [Inference: room noise reaches the caller's ear directly, not through the line].

| Condition | Parameters | Used for |
|---|---|---|
| **C0 Phone-clean** (default) | G.711 μ-law 8 kHz, no noise, no loss | all tracks, all tiers |
| **C1 Phone-realistic** | noise bed at 15 dB SNR ± 3 dB drift; Poisson bursts at 1 per minute; Gilbert-Elliott loss averaging 2% in 100–150 ms bursts; muffling on 20% of caller utterances [Fact: τ-Voice's realistic condition as summarized in the evidence base; the coding agent confirms exact values in tau2-bench and records them] | Track A in Core; all tracks in Full |
| **W Wideband diagnostic** | 16 or 24 kHz PCM, no codec | Track D codec-penalty subset only |
| Speakerphone echo | agent audio leaked into the caller channel at set attenuation | **v1.1**: needs live-loop mixing and a test of the harness's own echo path first |

Reports show every metric under C0 and the C1 − C0 delta with a paired CI. Per-effect decomposition is a diagnostic run, not part of any tier.

---

## 6. Transport

### 6.1 Live stream (primary)

Real-time WebSocket or WebRTC to each provider, 20 ms frames paced on the harness clock with drift correction and logged pacing slip [Fact: ASR harness design]. All headline numbers come from this mode.

### 6.2 Phone path (verification subset)

The harness owns two numbers on one trunk (Twilio or Telnyx; Open Question 5). The **caller leg** plays stimulus audio into the PSTN and records what comes back: on this transport **the ear is the caller leg's received audio**, which equals the §1.3 playout (applied on the agent leg) plus the trunk. The **agent leg** receives the call and bridges media to the system's adapter. Every system is reached the same way. Vendor-native SIP ingress is an optional variant labeled as such.

- A **loopback baseline** (caller leg to a harness echo endpoint) measures trunk round-trip and jitter before each phone-path run. Phone-path latencies are shown **raw and with the baseline subtracted**, both labeled.
- Scope: 10 scenarios per release, C0 only. Agreement with live stream is reported as **per-metric sign agreement of paired differences** across systems, not Spearman (seven systems give Spearman no power [Fact, arithmetic]).

### 6.3 Tick simulation — dropped from v1

τ-Voice's tick orchestrator is valid for APIs that index on audio time [Fact]. Cascades and self-hosted models run on wall-clock, so tick timing is not comparable across the architectures under decision [Inference]. The simulator-latency problem that motivated it is solved in §7. Cost: no direct τ-Voice comparability on task results.

---

## 7. Simulated caller (Track C)

- **Hears**: ear-side audio through the fixed reference ASR (streaming); never the agent's text [Fact: the draft, τ-Voice and Artificial Analysis critiques]. Where the provider emits an output transcript, the harness compares it with the reference-ASR text and reports **simulator mishearing rate**, so the ASR leak is quantified.
- **Thinks**: an LLM from a family with no voice system under test (Claude Sonnet 5.5; already Dograh's judge in the ASR harness [Fact]; OpenAI, Google and xAI are under test), temperature 0, given persona, goal, facts, the step script and the scripted self-correction. Prompts are versioned and hashed.
- **Speaks**: a TTS vendor with word timestamps, not used by any cascade under test, or the overlap is disclosed. Output goes through the channel model like any caller audio.
- **Knows the tool state.** Tools are mock, so the harness tells the simulator when a tool call is pending. **While a tool is pending, and for 1.5 s after its result, the simulator does not take the floor on silence** (a caller told "let me check" waits) [Inference]. Consequence: any agent speech during a tool wait is filler by construction, and dead air during tool waits measures the system, not the caller. Trade-off: the simulated caller is more patient than some real callers; stated in the run card.
- **Turn policy** outside tool waits: agent end of turn = ear inactivity ≥ 700 ms (above the 500 ms stop run so a stop and an end of turn are distinct). The caller speaks after a floor delay drawn from U(300, 800) ms (human-like, without emulating the 151 ms pre-end start humans show [Fact]). Generation starts speculatively from partial ASR at 300 ms of agent inactivity and is discarded if the agent continues. If the reply is not ready at the floor time, it is spoken when ready and `T_caller_think` is logged; a scenario whose caller p90 think time exceeds 1.5 s is flagged `slow_caller` and excluded from latency metrics.
- **No spontaneous interruptions.** If the agent resumes speaking after the simulator has started (a 700 ms silence that was not an end of turn), the overlap is tagged `unscripted_overlap`, excluded from Track A metrics and counted as a diagnostic; the simulator finishes its utterance.
- **Validation gate** [Fact: EVA-Bench pattern]: a trial is regenerated before scoring if the caller failed to state its goal entities within its first two turns (checked on its own text), produced broken audio, or hit a harness timeout. Regeneration is a whole-trial rerun as a new call (the gate can only be checked after the caller has spoken), capped at 2 regenerations [Proposed]; every attempt is kept with a `regenerated` flag and its cost counted; a third failure is the outcome `validation_failed`. Regeneration rate is reported; above 10% the scenario is reviewed before any run, never after.
- **Realism check** against 20–30 matched human-caller calls is v1.1.

---

## 8. Judges and classifiers

- **No LLM judge contributes to any headline metric in v1.** Order of evidence: mock database and tool log → exact and normalized matches → timing from the recording → rule-based classifiers → validated LLM classifiers (diagnostics only) → human review.
- **Rule-based classifiers** (Respond/Resume, success-claim patterns, recovery-request patterns) are auditable code. Each must be validated on a 200-item double-labeled set with Cohen's kappa ≥ 0.7 (human-vs-human agreement reported beside it) before its metric leaves the `classifier` label; pattern recall against the human labels is reported so phrasing the pattern set misses is visible.
- **LLM classifiers**, if added for diagnostics: one fixed judge for the whole table from a family with no system in it (never rotating per system [Inference: rotation changes the instrument]), order swap for pairwise items with disagreement as a tie, five reruns with flip rate < 2%, re-validation after any change. Every judged number carries a `judged` marker.

---

## 9. Statistics, sample sizes, run tiers

### 9.1 Rules

- Rates: Wilson 95% CIs. Percentiles: bootstrap, 1,000 resamples, clustered by scenario.
- **Comparisons are paired.** Pair key = (scenario id, trial index, condition); overlay events pair on (scenario id, overlay id, trial index); clusters are scenarios; a pair is dropped when either side is invalid for that metric and per-system drop counts are reported. Two systems on the same scenarios: bootstrap the paired difference, clustered by scenario; "distinguishable" only if that CI excludes zero. CI overlap is never used as a test. Holm correction across the pre-registered primary comparisons (at most six per run).
- Pass@1 and Pass^3 side by side with n. Percentiles are conditional on completion, with completion rate beside them [Fact: ASR harness rule].
- The CLI **planner** prints the half-width for a planned n and refuses to label a comparison "distinguishable" when the design cannot detect the effect size of interest.

### 9.2 Sample-size table [Fact, arithmetic]

| Need | n |
|---|---|
| Rate CI half-width ±13.6 / ±9.7 / ±6.9 / ±4.9 pts at p = 0.5 | 50 / 100 / 200 / 400 events |
| Detect a 10-point paired difference, 30% discordant pairs, 80% power, α 0.05 (McNemar) | ≈ 230 pairs |
| Detect a 20-point paired difference, 40% discordant | ≈ 75 pairs |
| p50 / p90 / p95 / p99 with usable bootstrap CI | 30 / 100 / 200 / 1,000 observations |

Core meets ±7 to ±10 points on pooled rates and detects 20-point differences; only Full detects 10-point differences on Pass@1 and gives per-class Hold rates.

### 9.3 Run tiers and cost

Cost per connected minute [Assumption, replaced by priced configs the coding agent fills from dated vendor pages]: speech-to-speech provider $0.10–0.30; cascade components $0.05–0.15; simulator (LLM + TTS + ASR) $0.10–0.25 on Track C calls; reference ASR on scripted calls $0.01; trunk $0.03 both legs; self-hosted GPU $1–3 per hour.

Event yield assumptions: 6 overlays per 3-minute Track A call; 30% of events lost to `ambiguous_end`/`not_injected`; 4 entities per 2-minute Track D call.

| Tier | Content per system | Call-minutes | Estimated cost per system | Purpose |
|---|---|---|---|---|
| **Smoke** | 5 Track A calls, 3 Track C scenarios × 1 trial, C0 | ≈ 25 | ≈ $5–15 | harness and adapter work |
| **Core** (release gate) | Track A: ≥ 100 valid events for each pooled metric (Yield, Hold-while-speaking, Hold-while-listening, E-END) → ≈ 95 overlay calls + 95 controls × 3 min, C0; half of them repeated under C1; Track C: the whole 40-scenario private pack × 3 trials, C0; Track D: four priority types × 50 items → 50 calls × 2 min; c = 1 | ≈ 570 + 285 + 360 + 100 ≈ **1,300** | ≈ **$300–700** | gate Dograh's stack; one reference S2S beside it |
| **Full** | Core + per-class n (≥ 100 per class) + all eight entity types + C1 on Track C + public-dev split + c = 8 pass on Track A + phone-path subset | ≈ 2,200 | ≈ $500–1,200; seven systems ≈ **$4k–8k** plus GPU time | vendor and architecture decision; publishable |

Annotation hours in v1: labels are by construction, so 0 hours for ground truth. Validation sets: three 200-item double-labeled sets (Respond/Resume, success claims, recovery requests) ≈ 18 hours. Scenario authoring: 40 private + 20 public scenarios ≈ 30 engineer-hours [Assumption].

---

## 10. Reporting

1. **Run card**: date, harness version, scenario-pack hash, every system's config hash and pre-registration timestamp, reasoning effort and turn-detection parameters, every threshold value, reference-ASR version and floors (entities and confirmations), simulator versions and prompt hash, simulator patience rule, calibration result, network baseline, instance type, iteration counts, vendors that received audio.
2. **Profile table**: every metric × system with CIs and n; `classifier` / `judged` markers; self-hosted flags.
3. **Floor-control charts**: Yield @1.0 s vs pooled Hold-while-speaking; Hold-while-listening vs Response rate @5 s.
4. **Latency tails**: p50 → p95 bars per ear-level latency; model-level twin and playout penalty beside each.
5. **Task table**: Pass@1, Pass^3, stale, pre-emptive, unsupported-claim (marked), policy, auth-bypass, by domain and by tool-latency bucket; interrupted vs clean split.
6. **Entity table** by type with the reference-ASR floor and the codec penalty.
7. **Frontier charts**: cost vs Pass@1; `T_turn_gap_ear` p50 vs Pass@1; gray dashed perpendicular guides at the best value on each axis; frontier systems listed below.
8. **Condition deltas**: C1 − C0 per metric with paired CIs.
9. **Failure clip browser**: playable stereo clips with the event timeline, filterable by failure class.
10. **Auto-generated caveats**: low-n percentiles, `ambiguous_end` / `not_injected` / `unmatched_control` / `contested` counts, `slow_caller` and `unscripted_overlap` counts, simulator mishearing and regeneration rates, phone-path disagreements, classifier validation status, missing cancel events.

There is **no composite score** in v1 (Decision Log D7).

---

## 11. Data governance and anti-gaming

- **Splits**: `public-dev` (debugging and tuning; anyone can run it) and `private-test` (scored only by Dograh; pre-registered configs only; always run whole). The private split, the private pattern extensions and the floor-set audio live in a separate private repository mounted at `DUPLEX_PRIVATE_DIR`; the harness repository never contains a private file, and pre-registration stores only the private tree hash [Fact, eng review D5 2026-11-04].
- **Pattern sets**: the public language pack ships the pattern and token lists; the private pack adds **held-out variants** (success-claim phrasings, recovery phrasings, backchannel tokens) not visible in public-dev, and pattern recall is measured against human labels (§8).
- **Versioning**: scenario pack, language pack, normalizer, reference ASR, simulator prompts, thresholds and harness all carry versions; the run stores all hashes; results across different hashes never aggregate and comparisons across them carry a flag. A changelog is required for any scoring change.
- **Refresh**: the private split is replaced when results are published externally or after 12 months, whichever first (quarterly refresh with new speakers is unaffordable before real recordings exist).
- **Memorization probes** (reference-disagreement and masked-entity items [Fact: Hume AI / Hugging Face]) are built in v1.1 when real recordings arrive; TTS stimuli cannot carry a reference-disagreement probe.
- **Audio disclosure**: the run card lists every third party that received caller or agent audio.
- **Publication policy** [Fact: Dograh decision 2026-11-04]: results, run cards and methodology are published, including Dograh's own stack; scenario packs, stimulus audio and the private pattern extensions are never published. Pre-registration records are therefore public artifacts, and the outside review in §2.3 applies to every public claim.

---

## 12. Deferred to v2 (not built in v1)

- Track B listener behavior: agent backchannel rate and timing; needs human reference distributions and matters little on a phone line.
- Track E long-call memory with rubric scoring (Audio MultiChallenge style [Fact]); needs judges.
- Track G output naturalness and paralinguistic appropriateness; needs validated audio-capable judges from a family not under test.
- Track H interruption jailbreaks (DuplexJail [Fact]) and proactive safety interruption; needs judged attack success.
- Real consented recordings, triple annotation, memorization probes and the human realism check (v1.1).
- Speakerphone echo condition and per-effect condition decomposition.
- LLM-driven spontaneous caller interruptions, backchannels and impatience during tool waits.
- Tick-indexed transport for τ-Voice comparability, if adapters exist for all architectures.
- Concurrency sweep beyond c = 8 and inflection-point detection.
- Non-English language packs.
