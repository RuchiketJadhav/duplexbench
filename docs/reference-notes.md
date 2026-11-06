# Reference notes (M0-T03)

Read-only notes from the clones in `reference/` (commits in `docs/licenses.md`), 2026-11-04. Each fact is
`path:line — quoted text`. Paths are relative to `reference/`. Found with `grep -rn`; no code was run.

## 1. Full-Duplex-Bench (`3e799c4`, CC-BY-NC-4.0: read only, re-implement, do not copy)

### TOR (take-over rate) — the exact rule in code

The rule is applied per sample to the **CrisperWhisper word chunks** of the model's output (`output.json["chunks"]`,
one entry per word, `timestamp = [start, end]` seconds). It is the same in three evaluators:

- `Full-Duplex-Bench/v1_v1.5/evaluation/eval_pause_handling.py:7` — `turn_duration_threshold = 1`
- `Full-Duplex-Bench/v1_v1.5/evaluation/eval_pause_handling.py:8` — `turn_num_words_threshold = 3`
- (identical literals) `eval_smooth_turn_taking.py:7-8`, `eval_user_interruption.py:7-8`.
- `eval_pause_handling.py:42-44` — `# if no transcription from CrisperWhisper， means model does not take turn` / `if len(segments_cw) == 0:` / `TOR = 0`
- `eval_pause_handling.py:46-53` — `if segments_cw[-1]["timestamp"][-1] == None: duration = (segments_cw[-1]["timestamp"][0] - segments_cw[0]["timestamp"][0])` else `duration = (segments_cw[-1]["timestamp"][-1] - segments_cw[0]["timestamp"][0])`
- `eval_pause_handling.py:54-60` — `if duration < turn_duration_threshold:` / `if len(segments_cw) <= turn_num_words_threshold:` / `TOR = 0` / `else: TOR = 1` / `else: TOR = 1`
- `eval_smooth_turn_taking.py:63-73` and `eval_user_interruption.py:100-112` — same rule; when `TOR == 1`, `latency = output_start_time - input_end_time` (`output_start_time = segments_cw[0]["timestamp"][0]`); negative latency is clipped to 0 (`eval_smooth_turn_taking.py:76-80`).

**Rule in words:** TOR = 0 if no words; else TOR = 0 iff (first-word-start → last-word-end duration **< 1 s**
**and** word count **≤ 3**); otherwise TOR = 1. Hand cases for M2-T09: silence → 0; 0.8 s, 1 word → 0; 1.5 s,
3 words → 1; 0.8 s, 4 words → 1. The code has no worked examples in comments.

Backchannel task uses a different, Silero-VAD-based rule:
- `eval_backchannel.py:14` — `time_threshold = 3`
- `eval_backchannel.py:63-65` — `if duration > time_threshold:  # means the model is formally speaking` / `TOR = 1`
- `eval_backchannel.py:101-111` — `num_words = len(curr_text)` / `if num_words > 3: TOR = 1` / `else: if duration < 1: if len(curr_text) <= 2: TOR = 0 else: TOR = 1` / `else: TOR = 1`
  (per VAD segment: > 3 s → takeover; else > 3 words → takeover; else < 1 s and ≤ 2 words → not; else takeover).

### v1.5 reaction labels
- `v1_v1.5/evaluation/instruction/behavior.txt:61` — `C_RESPOND`; `:65` — `C_RESUME`; `:69` — `C_UNCERTAIN_HANDLING`; `:74` — `C_UNKNOWN`.
- `behavior.txt:28` — `• If the model never speaks after the overlap: use "C_UNKNOWN".` (LLM-judged; see `eval_behavior.py`).

### v3 latency breakdown and tool scenarios
- `v3/analyze_tool_latency.py:144-145` — `first_response_latency = round(first_chunk_start - user_end, 3)` → `"first_response_latency_s"`.
- `v3/analyze_tool_latency.py:155-156` — `tool_call_latency = round(first_tool_start - user_end, 3)` → `"tool_call_latency_s"`.
- `v3/analyze_tool_latency.py:177-178` — `task_completion_latency = round(key_start - user_end, 3)` → `"task_completion_latency_s"` (`key_start` = first ASR chunk carrying the key info, found by an OpenAI judge, `identify_key_info` `:87`).
- `v3/latency_injector.py:48-80` — `LATENCY_PROFILES`: `"instant"` `fixed_ms=0`; `"fast"` `min_ms=50, max_ms=200`; `"normal"` `min_ms=200, max_ms=800`; `"slow"` `min_ms=1000, max_ms=3000`; `"degraded"` `min_ms=500, max_ms=2000, progressive_factor=1.5`; `"timeout_risk"` (further down).
- Tool data: `v3/benchmark_data_v2.json`, mock APIs `v3/mock_apis.py` (not read in detail; non-commercial licence, read only).

## 2. tau2-bench (`6fe66e9`, MIT)

All in `tau2-bench/src/tau2/`.

| Effect | path:line — quoted text |
|---|---|
| Background noise SNR | `voice_config.py:86` — `NOISE_SNR_DB = 15.0`; `:87` — `NOISE_SNR_DRIFT_DB = 3.0`; `:88` — `NOISE_VARIATION_SPEED = 0.5` |
| SNR reference | `voice_config.py:64-66` — `# Noise is scaled so that noise_level / this_value = 10^(-SNR/20).` / `SNR_SPEECH_REFERENCE_RMS = 3000.0` (fixed reference RMS, not measured speech) |
| Bursts | `voice_config.py:89-91` — `ENABLE_BURST_NOISE = True` / `BURST_NOISE_EVENTS_PER_MINUTE = 1.0` / `BURST_SNR_RANGE_DB = (-5.0, 10.0)  # Sample per burst from this range`; `:68` — `MIN_BURST_RMS = 100.0` |
| Frame drops (Gilbert-Elliott) | `voice_config.py:56-61` — `# Channel Effects - Frame Drops (Gilbert-Elliott model)` / `FRAME_DROP_RATE = 0.01  # Target loss rate (1%)` / `FRAME_DROP_BURST_DURATION_MS = 100  # Average burst duration in ms` / `FRAME_DROP_DURATION_MS = 150` |
| GE state loss probs | `voice/utils/probability.py:30` — `GE_DEFAULT_GOOD_STATE_LOSS_PROB = 0.0  # No loss in good state`; `:31` — `GE_DEFAULT_BAD_STATE_LOSS_PROB = 0.2  # 20% loss in bad state` |
| GE rates (continuous time, per second) | `voice/utils/probability.py:61-65` — `def r_rate` … `return 1.0 / avg_burst_duration_sec` (B→G); `:68-72` — `def p_rate` … `return self.r_rate * self.target_loss_rate / (h - self.target_loss_rate)` (G→B); `:121` — `p_transition = 1.0 - math.exp(-self.config.p_rate * duration_seconds)` |
| Preset actually used ("regular") | `user_simulation_voice_presets.py:181-183` — `# Frame drops - enabled with GE model params (1% loss rate, 100ms bursts)` / `"frame_drop_rate": 0.02,` / `"frame_drop_burst_duration_ms": 100,` (comment says 1 %, value is 2 %) |
| "heavy" overlay | `user_simulation_voice_presets.py:43-50` — `"frame_drop_rate": 0.03, "frame_drop_burst_duration_ms": 300, "enable_muffling": True, "muffle_probability": 0.5, "noise_snr_db": 10.0, "burst_noise_events_per_minute": 4.0, "burst_snr_range_db": (-5.0, 0.0)` |
| Muffling (dynamic) | `voice_config.py:92-97` — `ENABLE_DYNAMIC_MUFFLING = True` / `MUFFLE_PROBABILITY = 0.2` / `MUFFLE_SEGMENT_COUNT = 1` / `MUFFLE_SEGMENT_DURATION_MS = 500` / `MUFFLE_CUTOFF_FREQ = 1500.0` / `MUFFLE_TRANSITION_MS = 100`; filter `voice/synthesis/audio_effects/effects.py:283` — `b, a = signal.butter(4, normalized_cutoff, btype="low")` |
| Muffling (constant) | `voice_config.py:80-81` — `CONSTANT_MUFFLE_CUTOFF_FREQ = (1000.0  # Cutoff for permanent muffling (speaker facing away)` |
| Telephony | `voice_config.py:75-77` — `TELEPHONY_LOW_FREQ = 300`, `TELEPHONY_HIGH_FREQ = 3400`, `TELEPHONY_HEADROOM = 0.9`; `user_simulation_voice_presets.py:184-185` — `# Telephony - enabled (G.711 μ-law 8kHz compression)` |
| Out-of-turn speech | `voice_config.py:118` — `OUT_OF_TURN_SPEECH_EVENTS_PER_MINUTE = 0.7`; `:122` — `VOCAL_TICS_DURING_SPEECH_PROBABILITY = 0.1` |

**Gilbert-Elliott implication for BUILD_PLAN §6.** tau2's model is continuous-time with loss-in-bad
h = 0.2 (not 1.0). With the "regular" preset (rate 0.02, burst 100 ms): r = 10 /s, p = 10 × 0.02 / (0.2 − 0.02)
= 1.111 /s, π_B = p / (p + r) = 0.1 — 10 % of time in Bad, 20 % of those frames lost → 2 % average. The plan's
[Proposed] defaults (p = 0.0034, r = 0.1667 per 20 ms frame, h = 1.0, mean burst 120 ms) give the same 2 % average
loss but with every Bad frame lost (contiguous gaps). Which form to adopt is a human decision (CLAUDE.md §6 item 4);
the tau2 source values are recorded here.

## 3. turnbench (`38a6f87`, MIT code; data non-commercial)

Annotation shape (Hugging Face parquet; one row per conversation):
- `turnbench/turnbench/data.py:53-57` — `GOLD_COLUMNS = ["conversation_id"] + [f"speaker_{speaker}_annotation_{annotator}" for speaker in SPEAKERS for annotator in ANNOTATORS]`
- `data.py:47-48` — `ANNOTATORS = ("a", "b", "c")` / `SPEAKERS = (1, 2)`; `:58` — `AUDIO_COLUMNS = [f"speaker_{speaker}_audio" for speaker in SPEAKERS]` (separate channel per speaker, `{"bytes": ...}` WAV/FLAC, `:237`).
- `data.py:228-229` — each annotation column is a list of events read as `(event["start_s"], event["end_s"], event["label"], event["text"])`; `:66` — `Annotation = tuple[float, float, str, str]`.
- So one event JSON is `{"start_s": float, "end_s": float, "label": "<fine label>", "text": "<transcript>"}`.
- Fine → canonical labels: `gold.py:87-123` — `"Turn": ("Normal Turn", "Regular Turn", "Strong Floor Hold", "Bounded Response", "Filler", "Overlap")`, `"Laughter"`, `"AwkwardSilence"`, `"Interruption": ("Floor-taking Competitive Interruption", "Floor-taking Cooperative Interruption")`, `"NonFloorTakingInterruption"`, `"Backchannel": ("Acknowledgement Backchannel", "Continuer Backchannel", "Reaction Backchannel")`, `"NonContent": ("Non-Speech Noise", "Channel Bleed", "Speech, Non-Linguistic")`.
- Submission shape: `docs/SUBMISSION_FORMAT.md:16-33` — `{"schema_version": 1, "predictions": [{"conversation_id": "20", "speaker_1": {"eot": [12.84, ...], "interruption": [31.20]}, "speaker_2": {...}}]}`.
- Signed latency convention: `turnbench/score.py:18` — `events (TP, latency = t_pred - t)`; window `gold.py:150-151` — `TAU_PRE_S = 0.25` / `TAU_MAX_S = 3.00` (`SUBMISSION_FORMAT.md:64-65` — `[t − 0.25 s, min(t + 3.0 s, next)]`). Early predictions up to 250 ms give negative latency.
- Causality: `SUBMISSION_FORMAT.md:51-53` — "A timestamp is the time **by which all audio the decision depended on has been heard**".

## 4. pipecat (`0c6826a`, BSD-2-Clause)

Interruption-related frame classes (`pipecat/src/pipecat/frames/frames.py`):
- `:1425` — `class InterruptionFrame(SystemFrame):` — "Frame pushed to interrupt the pipeline. ... when a user starts speaking to cancel any in-progress bot output."
- `:1437` — `class UserStartedSpeakingFrame(SystemFrame):`; `:1448` — `class UserStoppedSpeakingFrame(SystemFrame):`
- `:1462` — `class UserTurnResponseCancelFrame(SystemFrame):` ("cancelling the speculative response for the current user turn"); `:1485` — `class EagerEndOfTurnCancelFrame(UserTurnResponseCancelFrame):`
- `:1496` / `:1507` — `UserMuteStartedFrame` / `UserMuteStoppedFrame`
- `:1636` — `class BotStartedSpeakingFrame(SystemFrame):`; `:1647` — `class BotStoppedSpeakingFrame(SystemFrame):`
- `:1794` — `ExternalFunctionCallCancelFrame`; `:1810` — `FunctionCallCancelFrame`; `:1279` — `CancelFrame`
- `StartInterruptionFrame` / `BotInterruptionFrame`: **not found** in `0c6826a` (`grep -rn "StartInterruptionFrame\|BotInterruptionFrame" src/`), nor in Dograh's submodule `b35b8e9` — `InterruptionFrame` is the single interruption frame.
- Turn observer events: `observers/turn_tracking_observer.py:80-81` — `"on_turn_started"`, `"on_turn_ended"`; `:199` — `on_turn_ended(turn_count, duration, was_interrupted)`.

Twilio serializer: `pipecat/src/pipecat/serializers/twilio.py`
- `:81` — `twilio_sample_rate: int = 8000`; `:83` — `auto_hang_up: bool = True`
- `:16` — `from pipecat.audio.utils import create_stream_resampler, pcm_to_ulaw, ulaw_to_pcm`
- `:214` — `answer = {"event": "clear", "streamSid": self._stream_sid}` (on `InterruptionFrame`)
- `:220` — `serialized_data = await pcm_to_ulaw(` (output: PCM → 8 kHz μ-law, base64 in `media.payload`)
- `:333` — `deserialized_data = await ulaw_to_pcm(` (input: 8 kHz μ-law → pipeline rate)
- Other telephony serializers alongside: `serializers/telnyx.py`, `plivo.py`, `vonage.py`, `exotel.py`, `genesys.py`.
- Dograh's pinned copy differs in line numbers (`dograh/pipecat/.../twilio.py:85, 227-229, 234, 335`); see `docs/dograh-ingress.md`.

## 5. stt-benchmark (`94f5672`, BSD-2-Clause)

VAD-anchored timing lives in pipecat's `STTService`, which stt-benchmark drives:
- `pipecat/src/pipecat/services/stt_service.py:623` — `async def _handle_vad_user_stopped_speaking(self, frame: VADUserStoppedSpeakingFrame):`
- `stt_service.py:641-642` — `speech_end_time = frame.timestamp - frame.stop_secs` / `await self.start_ttfb_metrics(start_time=speech_end_time)` (anchor = VAD stop time minus the VAD stop delay)
- `stt_service.py:663-665` — after `_stt_ttfb_timeout`, `await self.stop_ttfb_metrics(end_time=self._last_transcript_time)` (end = arrival of the last transcript)
- `stt-benchmark/src/stt_benchmark/observers/metrics_collector.py:19-20` — "TTFB is measured from when the user stops speaking (VAD detection) to when the first TranscriptionFrame is received." (the docstring says "first"; the pipecat code above measures to the *last* transcript before the timeout — reported as "TTFS" in `cli/report.py:139-141`).
- `metrics_collector.py:76-78` — `# Skip zero values (these are initialization metrics, not real TTFB)`
- `stt-benchmark/src/stt_benchmark/pipeline/benchmark_runner.py:177` — `vad_analyzer=SileroVADAnalyzer(params=VADParams(stop_secs=self.vad_stop_secs))`; `config.py:70` — `vad_stop_secs: float = 0.2`.

## 6. Silero VAD (`066409f`, MIT)

- `silero-vad/src/silero_vad/utils_vad.py:83` — `self.sample_rates = [8000, 16000]`; `:115` — "Supported values: 256 for 8000 sample rate, 512 for 16000" (32 ms windows at 8 kHz).
- `utils_vad.py:281-293` — `get_speech_timestamps(audio, model, threshold: float = 0.5, sampling_rate: int = 16000, min_speech_duration_ms: int = 250, ..., min_silence_duration_ms: int = 100, speech_pad_ms: int = 30, ...)` — pass `sampling_rate=8000` explicitly for the cross-check.

## 7. Not found / not read

- FDB v2 (`v2/`, JS orchestrator) — not read; not in the M0-T03 list.
- tau2-bench "database end-state grading" and domain data — not read in M0 (out of the M0-T03 list); to read before M4.
- Artificial Analysis and Twilio Media Streams docs pages are summarised in `docs/providers.md`.
