# Provider facts (M0-T04)

Date checked: **2026-11-04** for every row, unless a row says otherwise. Sources are public docs pages only. No API was called.
Tags: **[Fact]** means the cited page says it. **[Assumption]** means it is not verified. **not confirmed, blocked on: …** means a needed fact is missing.

Harness events used in the mapping tables: `first_audio_byte`, `cancel_or_truncate`, `output_transcript`, `tool_call`, `usage`.

Keys in `.env.example`: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `XAI_API_KEY`, `TTS_API_KEY`, `DUPLEX_PRIVATE_DIR`. All values are empty. So "reachable" below means "a key slot exists". No key has been tested.

## Summary

| # | System | g711 μ-law 8 kHz native? | Price unit | Key slot in .env.example |
|---|---|---|---|---|
| 1 | GPT-Live-1 (OpenAI) | **yes**: `{"type":"audio/pcmu","rate":8000}` | $0.05 / min voice session + backend tokens | yes (`OPENAI_API_KEY`); Free tier not supported |
| 2 | Moshi / PersonaPlex (self-hosted) | **no**: Opus 24 kHz over the WS protocol | GPU time (self-hosted) | n/a (PersonaPlex needs `HF_TOKEN`, which is not in .env.example) |
| 3 | OpenAI Realtime (`gpt-realtime-2.1`; `gpt-realtime` is deprecated) | **yes**: `{"type":"audio/pcmu"}` | per 1M tokens (audio $32 in / $64 out) | yes (`OPENAI_API_KEY`) |
| 4 | Gemini Live (`gemini-3.8-live`) | **no**: PCM16 only; input resampled from any rate; output 24 kHz | per 1M tokens or per min | yes (`GEMINI_API_KEY`) |
| 5 | xAI Grok Voice (`grok-voice-3.0`) | **yes**: `audio/pcmu` 8000 Hz | $0.08 / min | yes (`XAI_API_KEY`) |
| 6 | Pipecat cascade | yes, through `TwilioFrameSerializer` (converts to and from PCM) | sum of components | depends on the components chosen |
| 7 | Dograh stack | yes, via Pipecat telephony serializers [Assumption] | sum of components | unknown (no Deepgram key slot in .env.example) |

**Two findings the plan did not expect:**
- **GPT-Live-1 exists.** It is GA in the API on its own endpoint, `wss://api.openai.com/v1/live/sessions`. It is **not** the Realtime API. Its event names are different, and it has **no cancel or truncate event and no turn-detection parameters**.
- **`gpt-realtime` is deprecated.** It shuts down on 2027-01-20, and the replacement is `gpt-realtime-2.1`. M3 should pin `gpt-realtime-2.1`, or record the deprecation in the run card.

---

## 1. GPT-Live-1 (OpenAI, native full-duplex)

Docs:
- https://developers.openai.com/api/docs/models/gpt-live-1
- https://developers.openai.com/api/docs/guides/live
- https://developers.openai.com/api/docs/guides/voice-websockets?api=live (fetched as `.md`)
- https://developers.openai.com/api/docs/guides/live-conversations
- https://developers.openai.com/api/docs/guides/live-delegation
- Announcement: https://openai.com/index/introducing-gpt-live-1-in-the-api/

| Item | Value |
|---|---|
| Exists? | **Yes** [Fact]. "GPT-Live 1 is a full-duplex voice model for real-time conversations. It can listen and speak at the same time, and delegate reasoning and tool use to a backend agent." (model page) |
| Model ids | `gpt-live-1` [Fact, model page]. "GPT‑Live‑1 mini" is named only for ChatGPT (https://openai.com/index/introducing-gpt-live/); no API id is confirmed. A community post says GPT-Live-1.1 is "Coming soon to the API" (https://community.openai.com/t/gpt-live-1-1-is-rolling-out-with-new-voices-and-better-accents/1406704). Not confirmed as an API id. |
| Transport | WebRTC, WebSocket `wss://api.openai.com/v1/live/sessions`, and SIP [Fact, live guide + voice-websockets] |
| Audio formats | One format for both directions, set once in `session.audio.format` at startup and fixed for the session: `{"type":"audio/pcm","rate":24000}` (default), `{"type":"audio/pcm","rate":16000}`, `{"type":"audio/pcmu","rate":8000}`, `{"type":"audio/pcma","rate":8000}`. "A matching G.711 stream can pass through without conversion to PCM." [Fact, voice-websockets?api=live] |
| g711 μ-law 8 kHz native | **Yes** [Fact] |
| Turn detection | **None to configure.** "GPT-Live manages when to listen and speak as audio streams continuously." [Fact, voice-websockets]. Input controls are `session.input_audio.mute` and `session.input_audio.unmute`. "Muting input does not cancel backend work or stop generated speech." |
| Delegation | `delegation.type`: `client` or `responses`. If omitted or `null`, client mode is used [Fact, live-conversations]. Backend reasoning effort: not confirmed, blocked on: no reasoning-effort field is shown for the Live session itself. It belongs to the Responses backend model config [Assumption]. |
| Rate limits | Concurrent sessions: Free not supported, Build 50, Launch 300, Grow 500 [Fact, model page] |
| Pricing | "$0.05 per minute, billed per second. Backend model and tool usage is billed separately." "Session duration is not rounded up to the next whole minute." [Fact, model page; also https://developers.openai.com/api/docs/pricing] |
| Reachable with .env.example keys | yes (`OPENAI_API_KEY`) if the org is Build tier or higher; tier unknown |

Event mapping (WebSocket):

| Harness event | Provider event | Notes |
|---|---|---|
| send audio | `session.input_audio.append` (field `audio`, base64) | "Audio appends have no acknowledgment." |
| `first_audio_byte` | first `session.output_audio.delta` (`delta`) | "GPT-Live's primary WebSocket sends output audio without timing fields or an output-audio-done event." The harness must timestamp on receipt. |
| `cancel_or_truncate` | **not confirmed, blocked on: no cancel, truncate, or interruption event is documented** | Barge-in happens inside the model. The harness can detect a yield only from the audio stream (ear). Note for the M6 adapter: there is no cancel-event support. |
| `output_transcript` | `session.output_transcript.delta` (input side: `session.input_transcript.delta`) | "Transcript deltas have no item ID or event that marks a completed conversational turn." Each delta carries an interval on the session timeline. |
| `tool_call` | client delegation: `session.delegation.created` (`delegation.id`, `offset_ms`); Responses delegation: `response.event` envelope with nested Responses events | The app sends `session.thinking.append`, `session.commentary.append`, or `response.create` (live-delegation guide) |
| `usage` | `session.usage.updated` (`usage.seconds`, cumulative), final value in `session.closed` | "Voice-duration updates are cumulative snapshots; do not add them together." Backend tokens arrive via `response.event`. |
| lifecycle | `session.start`, `session.started`, `session.update`, `session.updated`, `session.close`, `session.closed`, `error` | |

---

## 2. Moshi (Kyutai) / PersonaPlex (NVIDIA), self-hosted full-duplex

Docs:
- https://github.com/kyutai-labs/moshi (README)
- https://github.com/kyutai-labs/moshi/blob/main/rust/protocol.md
- https://github.com/NVIDIA/personaplex (README)
- https://huggingface.co/nvidia/personaplex-7b-v1 (gated; not readable without login)

| Item | Moshi | PersonaPlex |
|---|---|---|
| Model ids | `kyutai/moshiko-pytorch-bf16`, `kyutai/moshika-pytorch-bf16` (and q8 / MLX / Candle variants) [Fact, README] | `nvidia/personaplex-7b-v1` [Fact, README]. The package is a fork of the `moshi` package (`pip install moshi/.`) |
| License | Code: MIT (Python, web client), Apache (Rust). Weights: **CC-BY 4.0** [Fact, README §License] | Code: MIT. Weights: **NVIDIA Open Model license**, accepted on Hugging Face before download [Fact, README] |
| Audio | Mimi codec "processes 24 kHz audio, down to a 12.5 Hz representation", frame 80 ms. Theoretical latency 160 ms; "practical overall latency as low as 200ms on an L4 GPU" [Fact, README] | same server (`python -m moshi.server --ssl …`). Opus is required (`libopus-dev`) [Fact, README] |
| Wire protocol | WebSocket binary messages. The first byte is the type: 0 Handshake, 1 Audio ("ogg frames containing opus encoded audio (24kHz, mono)"), 2 Text, 3 Control ("not used in full streaming mode"), 4 MetaData, 5 Error, 6 Ping [Fact, rust/protocol.md]. The README says the Python and Rust clients use "the same [protocol] as with the web UI" [Fact] | same protocol [Assumption: PersonaPlex keeps it unchanged] |
| g711 μ-law 8 kHz native | **No.** The adapter must transcode μ-law 8 kHz to Opus 24 kHz [Fact: the protocol states Opus 24 kHz] | **No** (same) |
| Turn detection | None. The model is full-duplex [Fact, README] | None. Persona is set by `--text-prompt` and voice by `--voice-prompt` (e.g. `NATF2.pt`) [Fact] |
| Event mapping | `first_audio_byte` = first MT=1 frame after the caller turn; `output_transcript` = MT=2 text tokens; `cancel_or_truncate`, `tool_call`, `usage`: **none exist** (no tools, no cancel event) | same; there is an offline mode `python -m moshi.offline --input-wav … --output-wav … --output-text …` [Fact] |
| Rate limits / pricing | Self-hosted: GPU cost only. The PyTorch build needs a GPU with about 24 GB [Fact, moshi/README] | same; there is a `--cpu-offload` flag |
| Reachable with .env.example keys | n/a (no key) | no: needs `HF_TOKEN` and an accepted license; neither is in .env.example |

---

## 3. OpenAI Realtime API (turn-based S2S)

Docs (reference fetched as markdown):
- https://developers.openai.com/api/reference/resources/realtime/client-events
- https://developers.openai.com/api/reference/resources/realtime/server-events
- https://developers.openai.com/api/docs/guides/realtime-conversations
- https://developers.openai.com/api/docs/guides/realtime-vad
- Model pages: https://developers.openai.com/api/docs/models/gpt-realtime-2.1, https://developers.openai.com/api/docs/models/gpt-realtime
- Deprecations: https://developers.openai.com/api/docs/deprecations
- (platform.openai.com/docs/api-reference/realtime-* now redirects to developers.openai.com)

| Item | Value |
|---|---|
| Model ids | `gpt-realtime-2.1` (current; "improved … interruption behavior"; configurable reasoning effort), `gpt-realtime-2.1-mini`, `gpt-realtime-2`, `gpt-realtime-1.5`. `gpt-realtime` (snapshot `gpt-realtime-2025-08-28`) has **Status: Deprecated** and shuts down on **Jan 20, 2027**; the replacement is `gpt-realtime-2.1` [Fact, model page + deprecations] |
| Audio formats (GA) | `session.audio.input.format` / `session.audio.output.format` ∈ `{type:"audio/pcm", rate:24000}` ("Only a 24kHz sample rate is supported"), `{type:"audio/pcmu"}` ("The G.711 μ-law format"), `{type:"audio/pcma"}` [Fact, client-events]. The default is PCM 16-bit 24 kHz mono. |
| g711 μ-law 8 kHz native | **Yes**, as `audio/pcmu`. The candidate name `input_audio_format:"g711_ulaw"` appears only on the legacy "Realtime Session" domain type (`input_audio_format: optional "pcm16" or "g711_ulaw" or "g711_alaw"`) in https://developers.openai.com/api/reference/resources/realtime, **not** in the GA `session.update` schema. Use `audio.input.format.type`. |
| Turn detection (`session.audio.input.turn_detection`) | `ServerVad {type, create_response, idle_timeout_ms, 4 more}` or `SemanticVad {type, create_response, eagerness, interrupt_response}` or `null`. Defaults: `type` `"server_vad"`; `threshold` 0.5; `prefix_padding_ms` 300; `silence_duration_ms` 500; `create_response` true; `interrupt_response` true; `idle_timeout_ms` optional, null. `eagerness` `low`/`medium`/`high`/`auto` (default `auto` = medium; max timeouts 8 s / 4 s / 2 s) [Fact, client-events] |
| Rate limits (`gpt-realtime-2.1`) | Build 400 RPM / 200,000 TPM; Launch 10,000 / 4,000,000; Grow 20,000 / 15,000,000 [Fact, model page]. Runtime: `rate_limits.updated` (`limit`, `name`, `remaining`, `reset_seconds`) |
| Pricing (`gpt-realtime-2.1`, per 1M tokens) | Audio: in $32, cached in $0.40, out $64. Text: in $4, cached $0.40, out $24. Image in $5 [Fact, model page + https://developers.openai.com/api/docs/pricing]. `gpt-realtime` (deprecated): audio the same; text out $16. `gpt-realtime-2.1-mini` audio: in $10 / out $20 [Fact, pricing page] |
| Reachable with .env.example keys | yes (`OPENAI_API_KEY`); untested |

Verification of the M0-T04 candidates (as found on the reference pages):

| Candidate | Status | As found |
|---|---|---|
| `session.update{input_audio_format,output_audio_format,turn_detection{…}}` | **Changed in GA** | `session.update` exists. Fields are now `session.audio.input.format`, `session.audio.output.format`, `session.audio.input.turn_detection`. `turn_detection` has `type:"server_vad"`, `interrupt_response`, `create_response`, `silence_duration_ms`, `prefix_padding_ms`, `threshold`, plus `idle_timeout_ms` |
| `input_audio_buffer.append` | confirmed | `## input_audio_buffer.append` (client-events) |
| `response.output_audio.delta` vs `response.audio.delta` | confirmed: `response.output_audio.delta` | `response.audio.delta`: 0 occurrences on the reference pages |
| `input_audio_buffer.speech_started` | confirmed | fields `audio_start_ms`, `event_id`, `item_id`, `type` |
| `response.done{status:"cancelled"}` | confirmed | `status: optional "completed" or "cancelled" or "failed" or 2 more`; `status_details.reason`: "For a `cancelled` Response, one of `turn_detected` (the server VAD detected a new start of speech) or `client_cancelled` (the client sent a cancel event)" |
| `conversation.item.truncate` | confirmed | "useful when the user interrupts to truncate audio that has already been sent to the client but not yet played"; fields `item_id`, `content_index`, `audio_end_ms`; ack `conversation.item.truncated`. "Truncating audio will delete the server-side text transcript" |
| `response.function_call_arguments.done` | confirmed | fields `arguments, call_id, event_id, item_id, name, output_index, response_id, type` |
| `conversation.item.create{type:"function_call_output"}` | confirmed | item `type: "function_call_output"` in the `conversation.item.create` schema |
| `response.output_audio_transcript.done` | confirmed | also `response.output_audio_transcript.delta` |
| `response.done.response.usage` | confirmed | `usage`: `total_tokens`, `input_tokens`, `output_tokens`, `input_token_details{audio_tokens, text_tokens, cached_tokens, cached_tokens_details, image_tokens}`, `output_token_details{audio_tokens, text_tokens}` |
| `output_audio_buffer.clear` WebRTC-only | confirmed, **"WebRTC/SIP Only"** | "Emit to cut off the current audio response… This event should be preceded by a `response.cancel` client event". `output_audio_buffer.started`, `.stopped`, `.cleared` are also WebRTC/SIP only. Guide: "In WebRTC and SIP connections the server … will automatically truncate unplayed audio"; on WebSocket, "stop playback immediately and send a `conversation.item.truncate` event" (realtime-conversations) |

Harness mapping: `first_audio_byte` = first `response.output_audio.delta` of a response. `cancel_or_truncate` = `input_audio_buffer.speech_started`, then `response.done` (`status:"cancelled"`, reason `turn_detected`), plus client `conversation.item.truncate`. Manual cancel uses `response.cancel`. `output_transcript` = `response.output_audio_transcript.done`. `tool_call` = `response.function_call_arguments.done`. `usage` = `response.done.response.usage`.

---

## 4. Gemini Live API (turn-based S2S)

Docs (fetched as `.md.txt`):
- https://ai.google.dev/gemini-api/docs/live
- https://ai.google.dev/gemini-api/docs/live-guide
- https://ai.google.dev/api/live
- https://ai.google.dev/gemini-api/docs/live-session
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/rate-limits

| Item | Value |
|---|---|
| Model ids | `gemini-3.8-live`, `gemini-3.8-live-extended-thinking`, `gemini-3.1-flash-live-preview` (pricing page). Older: `gemini-2.5-flash-native-audio-preview-12-2025` [Fact] |
| Transport | Stateful WSS `/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent` [Fact, api/live] |
| Audio | Input "raw 16-bit PCM audio, 16kHz, little-endian". Output "raw 16-bit PCM audio, 24kHz". "Input audio is natively 16kHz, but the … API will resample if needed so any sample rate can be sent", with MIME such as `audio/pcm;rate=16000` [Fact, live + live-guide] |
| g711 μ-law 8 kHz native | **No**: there is no μ-law/PCMU on any fetched page. The adapter must decode μ-law to PCM16. It may send `audio/pcm;rate=8000` (resampled by Google) [Fact for resampling; the μ-law absence is from the fetched pages] |
| Turn detection (`realtimeInputConfig.automaticActivityDetection`) | `disabled` (default: enabled), `startOfSpeechSensitivity` (default `START_SENSITIVITY_HIGH`), `endOfSpeechSensitivity` (default `END_SENSITIVITY_HIGH`), `prefixPaddingMs`, `silenceDurationMs` (numeric defaults not stated). `activityHandling` default `START_OF_ACTIVITY_INTERRUPTS`. Manual mode: `activityStart`/`activityEnd` [Fact, api/live] |
| Session limits | "audio-only sessions are limited to 15 minutes" without context-window compression; connection lifetime "around 10 minutes" (`GoAway`) [Fact, live-session] |
| Rate limits | not confirmed, blocked on: Live limits are shown per project in AI Studio (https://aistudio.google.com/rate-limit), not on the docs page |
| Pricing (`gemini-3.8-live` family, paid tier) | Input: $0.75 text; **$3.00 per 1M tokens or $0.005/min audio**; $1.00 or $0.002/min image/video. Output (incl. thinking): $4.50 text; **$12.00 per 1M tokens or $0.018/min audio**. Free tier: free of charge [Fact, pricing] |
| Reachable with .env.example keys | yes (`GEMINI_API_KEY`); untested |

Mapping (`BidiGenerateContentServerMessage`): `first_audio_byte` = first `serverContent.modelTurn` part with inline audio. `cancel_or_truncate` = `serverContent.interrupted: true` ("If the client is playing out the content in real time, this is a good signal to stop and empty the current playback queue"); an interrupted turn goes "interrupted > turn_complete" with no `generationComplete`. `output_transcript` = `serverContent.outputTranscription` (input: `inputTranscription`). `tool_call` = `toolCall` (reply `toolResponse`; also `toolCallCancellation`). `usage` = `usageMetadata` (`totalTokenCount`, `responseTokensDetails[]`, …) [Fact, api/live]. There is no client truncate event.

---

## 5. xAI Grok Voice (Voice Agent / Speech to Speech API)

Docs (markdown links from https://docs.x.ai/llms.txt):
- https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech
- https://docs.x.ai/developers/rest-api-reference/inference/voice
- https://docs.x.ai/developers/pricing
- https://docs.x.ai/developers/rate-limits

| Item | Value |
|---|---|
| Model ids | `grok-voice-latest` ("Alias for `grok-voice-3.0`"), `grok-voice-3.0` ("Flagship voice model"), `grok-voice-2.0` (deprecated) [Fact] |
| Transport | `wss://api.x.ai/v1/realtime?model=grok-voice-latest`; also SIP (`call_id`). The protocol uses the same event shapes as OpenAI Realtime [Fact] |
| Audio | `audio.input.format.type` / `audio.output.format.type` ∈ `audio/pcm` (rates 8000, 16000, 22050, 24000 default, 32000, 44100, 48000), `audio/pcmu` (8000 Hz), `audio/pcma` (8000 Hz), `audio/opus` (24000 Hz). Input and output are set separately. Transport is `json` (base64) or `binary` [Fact] |
| g711 μ-law 8 kHz native | **Yes** (`audio/pcmu`) [Fact] |
| Turn detection | `turn_detection.type`: `"server_vad"` or `null`; `turn_detection.idle_timeout_ms` (default `null`). Item-level `interruptible` (default `true`; "When `false`, caller audio is dropped until playback completes"). `reasoning.effort`: `"high"` (default) or `"none"`. threshold / silence / prefix params: **not documented** [Fact: absent from the session parameter table] |
| Rate limits | `grok-voice-3.0` concurrent sessions: T0 10, T1 20, T2 50, T3 100, T4 200 ("Speech to Speech is limited by concurrent sessions alone") [Fact] |
| Pricing | Speech to Speech (grok-voice-3.0): **$0.08 / min ($4.80 / hr)** plus $0.004 / text input [Fact, pricing] |
| Reachable with .env.example keys | yes (`XAI_API_KEY`); untested |

Mapping: `first_audio_byte` = first `response.output_audio.delta` (docs also name `response.audio.delta` as an alias). `cancel_or_truncate` = `input_audio_buffer.speech_started` ("In VAD mode, interruptions are automatic"); client `conversation.item.truncate` → `conversation.item.truncated`; manual `response.cancel`. A `response.done` cancelled status is **not confirmed, blocked on: no status field documented**. `output_transcript` = `response.output_audio_transcript.done`. `tool_call` = `response.function_call_arguments.done` (reply `conversation.item.create` with `function_call_output`). `usage`: **not confirmed, blocked on: no usage fields documented on `response.done`**. Billing is per minute, so the harness can compute cost from session duration.

---

## 6. Pipecat cascade

Docs:
- https://docs.pipecat.ai/pipecat/fundamentals/interruptions
- https://docs.pipecat.ai/pipecat/learn/speech-input
- https://docs.pipecat.ai/api-reference/server/services/serializers/twilio
- https://docs.pipecat.ai/api-reference/server/frames/system-frames
- source: https://github.com/pipecat-ai/pipecat/blob/main/src/pipecat/serializers/twilio.py (main branch, read 2026-11-04)

| Item | Value |
|---|---|
| Cost | Pipecat is open source and free. STT, LLM, and TTS vendors are config choices; their versions and prices are recorded per run (spec §2.1). Price = sum of components. |
| Telephony | `TwilioFrameSerializer(stream_sid, call_sid, account_sid, auth_token, params=InputParams(twilio_sample_rate=8000, sample_rate=None, auto_hang_up=True, ignore_rtvi_messages=True, resampler_clear_after_secs=0.2))`. "Twilio uses 8kHz mu-law (PCMU) audio encoding. The serializer automatically converts between this format and Pipecat's PCM audio." DTMF → `InputDTMFFrame` [Fact, serializer doc] |
| Interruption → Twilio | In source, `InterruptionFrame` → `{"event": "clear", "streamSid": …}`. Audio out → `{"event":"media","streamSid":…,"media":{"payload":…}}` [Fact, twilio.py L213-231]. The serializer source shows no `mark` send or receive handling. |
| Interruption frames | `InterruptionFrame` (SystemFrame, broadcast upstream and downstream; LLM cancelled, TTS buffers cleared, transport flushes unplayed audio). `FunctionCallCancelFrame` for calls with `cancel_on_interruption=True`. `UserStartedSpeakingFrame` / `UserStoppedSpeakingFrame` (turn strategy), `VADUserStartedSpeakingFrame` / `VADUserStoppedSpeakingFrame` (raw VAD). Also listed: `BotStartedSpeakingFrame`, `BotStoppedSpeakingFrame`, `UserTurnResponseCancelFrame`, `EagerEndOfTurnCancelFrame`, `UserBackchannelFrame`, `UserMuteStartedFrame`/`UserMuteStoppedFrame` [Fact, interruptions + system-frames]. Event `on_assistant_turn_stopped` reports `message.interrupted` and the spoken-so-far `message.content` |
| Turn detection | `VADParams(confidence=0.7, start_secs=0.2, stop_secs=0.2, min_volume=0.6)` (doc example; `start_secs` and `stop_secs` "default: 0.2"). Turn end default: Smart Turn (`LocalSmartTurnAnalyzerV3`). Interruptions are "enabled by default" (`VADUserTurnStartStrategy(enable_interruptions=False)` disables them; `MinWordsUserTurnStartStrategy(min_words=3)`) [Fact] |
| Harness mapping | `first_audio_byte` = first audio frame out of the transport (`BotStartedSpeakingFrame`) [Assumption: exact frame is an adapter choice]. `cancel_or_truncate` = `InterruptionFrame` / Twilio `clear`. `output_transcript` = `TTSTextFrame` / `on_assistant_turn_stopped`. `tool_call` = function-call frames (`FunctionCallsStartedFrame`, `FunctionCallResultFrame`). `usage` = `MetricsFrame` [Assumption: usage fields not checked] |
| Reachable with .env.example keys | depends on the component choice; unknown |

---

## 7. Dograh stack

- Open source, Pipecat-based cascade. Deepgram STT plus a litellm-routed LLM [Fact, project memory, per SPEC §2.1].
- Ingress details live in `docs/dograh-ingress.md` (M0-T05, written 2026-11-04): harness-as-carrier is scriptable, FALLBACK B not required. Twilio path is 8 kHz μ-law; Dograh emits `media` and `clear` (→ `cancel_or_truncate`) and never `mark`.
- Formats and events: inherited from Pipecat (§6) when it uses the Twilio serializer [Assumption].
- Pricing: sum of components (Deepgram + LLM via litellm + TTS + hosting).
- Reachable with .env.example keys: **unknown**. There is no `DEEPGRAM_API_KEY` slot, and the Dograh endpoint is not configured.

---

## 8. Deepgram Nova-3 streaming (reference ASR, decision B11)

Checked 2026-11-06. Sources: developers.deepgram.com/reference/speech-to-text-api/listen-streaming (AsyncAPI
reference served as markdown), deepgram.com/pricing.

- Endpoint: `wss://api.deepgram.com/v1/listen` with query parameters. Header `Authorization: Token <DEEPGRAM_API_KEY>`.
- Audio in: `encoding` enum includes `linear16` and `mulaw`; `sample_rate` free; `channels` (default 1). So 8 kHz
  μ-law and 8 kHz PCM16 are both accepted natively. The harness sends `encoding=linear16&sample_rate=8000&channels=1`
  (its ear frames are int16).
- Parameters used: `model=nova-3`, `language=en`, `interim_results=true` (default false), `endpointing` (default 10,
  ms of silence before a final), `utterance_end_ms` (optional UtteranceEnd message), `punctuate=false`,
  `smart_format=false`, `numerals=false` (all default false, left so the normalizer sees spoken forms),
  `version=latest` (default; no other value documented for Nova-3).
- Client control messages (JSON text frames): `{"type":"Finalize"}` flushes pending audio into a final Results
  message (`from_finalize: true`), `{"type":"CloseStream"}` ends the stream, `{"type":"KeepAlive"}` keeps it open.
- Server messages: `Results` with `is_final`, `speech_final`, `start`, `duration` (seconds), `channel.alternatives[0]`
  holding `transcript`, `confidence` and `words[]` of `{word, start, end, confidence, punctuated_word?}` (times in
  seconds from stream start); `metadata.request_id`, `metadata.model_uuid`, `metadata.model_info{name, version, arch}`;
  `Metadata` at close (`duration`, `channels`); `UtteranceEnd`; `SpeechStarted` (with `vad_events=true`).
- Harness event mapping (fix-24, 2026-11-07): `stream()` releases a word when it arrives in an `is_final`
  Results message (`final=True`, exactly once, never changed). `stream(provisional=True)` also releases
  interim words (`final=False`) the first time they appear at their position in the chunk, and again only
  when their text changes. Transcripts use final words only (`engine.settled`). Measured 2026-11-07 on
  54.6 s of speech: finals arrive once per 3 to 5 s chunk (partial p90 3990 ms finals-only), interims
  every few hundred ms. `finish()` sends Finalize then CloseStream and waits for the final Results and Metadata.
- Finalize on stop (fix-40, 2026-11-08; Dograh parity): pipecat (services/deepgram/stt.py) sends `{"type":"Finalize"}`
  when its local VAD reports the user stopped, and Dograh runs Deepgram with endpointing=100, interim_results=true,
  punctuate=true, nova-3-general, language multi, turn-taking from Silero VAD (stop_secs 0.2). The harness now defaults
  `endpointing=100`, sends Finalize mid-stream (`DeepgramStreamer.finalize()`, repeatable, stream stays open) on every
  agent stop of the ear segmenter (500 ms hold), and gates calibration on TTFS (Time To Final Segment = final receipt
  minus speech end, speech end = VAD stop minus stop_secs). Pipecat's stt-benchmark measured Deepgram nova-3-general
  at TTFS p50 247 ms, p95 298 ms, p99 326 ms with that scheme (source: reference/stt-benchmark/README.md, results table;
  definition in reference/stt-benchmark/docs/measuring-ttfs.md). punctuate stays false here (raw words for matching).
- Measured live 2026-11-08 (calibration clip, 13 utterances, 800 ms pauses, this laptop; log in
  results/dg_finalize_log-20261108.json): with endpointing=100 Deepgram's own `is_final` arrives 460 to 880 ms after
  speech end, before the harness Finalize reply (sent at speech end + 500 ms hold, reply at about + 800 ms). The
  Finalize round trip is about 300 ms, which is the network RTT from here. The Finalize reply is empty (0 words,
  `from_finalize: true`) when the words were already flushed. First calibration run: TTFS p50 801 ms, p90 3893 ms,
  wait after the stop p50 301 / p90 401 ms, with 2 of 13 utterances at 4.6 and 5.6 s (words came only with the next
  chunk final). Second run: all 13 between 461 and 880 ms. `finish()` once hit its 5 s timeout while the test suite
  loaded the CPU, and passed on the rerun.
- Calibration clip and diagnostics log (fix-41, 2026-11-08): the default calibration clip is now
  tests/fixtures/refasr_calib_speech_3min.wav (195 s, 42 sentences, 800 ms pauses; about 0.025 USD per calibration at
  $0.0077 per audio minute, the projection follows the clip's duration), so one late final out of 42 no longer decides
  the TTFS p90. A failed reference-ASR stage (gate or any `RefASRTransportError`, the `finish()` timeout included) writes
  `calibration-<ts>.refasr_log.json` beside the calibration JSON: every server message with `_arrival_ms`, the stop and
  Finalize send times, per-utterance onset, speech end, last final word and its return time, TTFS, and the reason
  (API key scrubbed; `--keep-refasr-log` writes it on a pass too). The `finish()` timeout message now names the last
  server message type (`Results interim|is_final|from_finalize`, `Metadata`) and the ms since it arrived, or says
  that none arrived.
- Version pinning: none offered by the API beyond `version=latest`. The harness records `model_uuid` and
  `model_info.version` per stream and refuses a run whose uuid differs from the pinned one (B11).
- Price (pay-as-you-go, 2026-11-06): Nova-3 monolingual streaming $0.0077 per audio minute; pre-recorded $0.0043.
  Growth tier $0.0065 / $0.0036. Billed per second of audio.
- Rate limits: pay-as-you-go allows 150 concurrent streaming connections (pricing FAQ); the harness uses one.

## 9. Simulator LLM for the test run (decision B12)

Checked 2026-11-06 at platform.openai.com/docs/models and /docs/pricing (served as markdown).
- `gpt-4.1` and its snapshot `gpt-4.1-2025-04-14` are marked Deprecated. Not used.
- `gpt-5.4-mini`: default snapshot `gpt-5.4-mini-2026-03-17`; Chat Completions supported; `reasoning_effort`
  supports none (default), low, medium, high, xhigh; $0.75 input / $4.50 output per 1M tokens. Whether the model
  accepts `temperature` is not stated on its page, so the harness does not send it (first smoke confirms).
- Sent through `simulator.llm` as `kind: openai_compat`, `base_url: https://api.openai.com/v1`,
  `extra_body: {reasoning_effort: none}`. Test run only (spec §7 family rule), see B12.

## 10. Fish Audio text-to-speech (caller voice and stimuli, decision B13)

Checked 2026-11-06 at docs.fish.audio (pages served as markdown: api-reference/endpoint/openapi-v1/text-to-speech,
text-to-speech-stream-with-timestamps, text-to-speech-live-with-timestamps, features/text-to-speech,
features/text-to-speech/drama-3, developer-guide/models-pricing/pricing-and-rate-limits, models-overview).

- Endpoints: `POST https://api.fish.audio/v1/tts` (audio only), `POST /v1/tts/stream/with-timestamp`
  (Server-Sent Events: each `data:` line is JSON with `audio_base64`, `content`, `chunk_seq`,
  `chunk_audio_offset_sec`, `alignment` or null), `wss://api.fish.audio/v1/tts/live` and
  `/v1/tts/live/with-timestamp` (MessagePack frames, text streamed in). Header `Authorization: Bearer <key>`.
  The model is chosen by the HTTP header `model`; a missing or misspelled header falls back to `s2.1-pro`
  without an error.
- Models (header values): `s2.1-pro` (recommended for production, TTFA and DPA guarantees), `s2.1-pro-free`
  (same model, $0, no guarantees), `drama-3` (directed delivery: `[bracket]` directions, sounds, pauses,
  paired tags such as `<slow>`, multi-speaker), `s2-pro` (previous), `s1` (deprecated, retires 2026-12-31).
- Word timestamps: `alignment.segments[] = {text, start, end}` in seconds from the start of that text chunk;
  add `chunk_audio_offset_sec` for the whole-clip timeline. Snapshots are cumulative per `chunk_seq`:
  replace, never append. A trailing event with empty audio may carry the final correction. Whether
  `drama-3` serves the with-timestamp routes is not stated on its page [Assumption: yes, same request];
  the first paid smoke confirms.
- Request fields used by the harness: `text`, `reference_id` (voice id), `format: pcm` (raw 16-bit mono;
  sample rates 8, 16, 24, 32 or 44.1 kHz; the harness asks for 16000), `sample_rate`, `latency`
  (`balanced` default, about 300 ms to first audio; `normal` is more stable), `prosody.speed` (the
  scenario's `speaking_rate`), `normalize: true`, `language: en`. `temperature` / `top_p` left at the
  defaults (0.7) and recorded.
- Public EN-US voices named on the Drama 3 page: Adrian `bf322df2096a46f18c579d0baa36f41d` (male),
  Delia `c70cfb98867a4bfb9ae20d2401257b92` (female), Sheila `f3a2b90078d54a65af4b95f63bc7798e` (female).
- Price (input text, per million UTF-8 bytes): `drama-3` $50.00 (launch price $10.00 until 2026-11-30),
  `s2.1-pro` $15.00, `s2.1-pro-free` $0. One English character is one byte, so $15/M bytes = $0.015 per
  1,000 characters. Billing errors (401, 402, 503) are not billed.
- Rate limits: Starter tier (under $100 paid) 5 concurrent requests. The harness uses one at a time.
- Latency claim: the models overview states 100 ms time-to-first-audio for the S2 family; no figure is
  given for `drama-3`. The simulated caller measures `T_caller_think`, so the live smoke will show it.

## A. Artificial Analysis: speech-to-speech methodology

Docs: https://artificialanalysis.ai/methodology/speech-to-speech-benchmarking (leaderboard: https://artificialanalysis.ai/speech-to-speech)

- **TTFA**: "Average number of seconds required to generate the first token of audio output, measured across the Big Bench Audio question set." [Fact]. TTFA is measured on single-question audio files (Big Bench Audio), not on conversations.
- **Forced endpointing**: **not documented on the page.** The page never says how the end of the input audio is signalled (VAD vs manual commit). Status: not confirmed, blocked on: methodology page silent.
- **How the simulated customer hears the model (τ-Voice agentic track)**: "The customer is simulated by GPT-4.1 (gpt-4.1-2025-04-14, temperature 0), which writes each customer turn. Its turns are spoken with ElevenLabs eleven_v3, and it hears the tested model through Deepgram Nova-3 transcription." The grader is GPT-4.1, temperature 0. "The same customer and grader are used for every model." [Fact]
- Other: Conversational Dynamics uses a subset of Full Duplex Bench v1/v1.5 (pause handling, turn taking, user interruption, backchannel). Index v2.0 (Aug 2026) "Replaced Conversational Dynamics (Full Duplex Bench) with Task Success Rate in the index" [Fact].

## B. Twilio Media Streams WebSocket messages

Docs: https://www.twilio.com/docs/voice/media-streams/websocket-messages (page modified 2026-10-08)

Twilio → server:

```json
{"event":"connected","protocol":"Call","version":"1.0.0"}
{"event":"start","sequenceNumber":"1","start":{"accountSid":"AC…","streamSid":"MZ…","callSid":"CA…","tracks":["inbound"],"mediaFormat":{"encoding":"audio/x-mulaw","sampleRate":8000,"channels":1},"customParameters":{"FirstName":"Jane"}},"streamSid":"MZ…"}
{"event":"media","sequenceNumber":"4","media":{"track":"inbound","chunk":"2","timestamp":"5","payload":"<base64>"},"streamSid":"MZ…"}
{"event":"stop","sequenceNumber":"5","stop":{"accountSid":"AC…","callSid":"CA…"},"streamSid":"MZ…"}
{"event":"dtmf","streamSid":"MZ…","sequenceNumber":"5","dtmf":{"track":"inbound","digit":"1"}}
{"event":"mark","sequenceNumber":"4","streamSid":"MZ…","mark":{"name":"my label"}}
```

Server → Twilio (bidirectional `<Connect><Stream>` only):

```json
{"event":"media","streamSid":"MZ…","media":{"payload":"<base64 mulaw/8000>"}}
{"event":"mark","streamSid":"MZ…","mark":{"name":"my label"}}
{"event":"clear","streamSid":"MZ…"}
```

- `mediaFormat`: encoding "Value is always `audio/x-mulaw`", sampleRate "Value is always `8000`", channels "Value is always `1`".
- `media.timestamp`: "Presentation Timestamp in Milliseconds from the start of the stream". `chunk` starts at 1 and increments. `sequenceNumber` is a string.
- `customParameters`: "populated with the values you provided when starting the stream" (`<Stream>` TwiML parameters or the Stream resource).
- **mark**: "When that `media` message's playback is complete, Twilio sends a `mark` message to your server using the same `mark.name`". It is also sent back "when the audio ends (or if there is no audio buffered)".
- **clear**: "empties all buffered audio and causes any `mark` messages to be sent back". These returned marks tell the app which `media` messages "have been cleared and will not be played".
- `dtmf` and inbound `mark` are bidirectional-only.
- Outbound media: "The payload must be encoded `audio/x-mulaw` with a sample rate of `8000` and must be base64 encoded. **The audio can be of any size.**" It must not contain file-type header bytes. "The media messages are buffered and played in the order received."
- **20 ms chunking: not documented on this page.** The page gives no chunk duration for either direction ("The audio can be of any size" for outbound). The 20 ms figure stays third-party only.

## Reference client behaviour (OpenAI Realtime over WebSocket; M3-T03, 2026-11-05)

`output_audio_buffer.clear` is WebRTC/SIP only, so the harness (adapters/openai_realtime.py) acts as the documented WebSocket client: on `input_audio_buffer.speech_started` while agent audio is in flight it emits `cancel_or_truncate`, the runner flushes playout, the adapter drops further deltas of that item still in flight, and sends `conversation.item.truncate{item_id, content_index, audio_end_ms}`. `audio_end_ms` = wall time since the item's first delta, capped at the audio received (the adapter does not see the playout queue; the error is at most the jitter buffer). A later `response.done{status:"cancelled"}` for the same response is not a second cancel. A provider `error` whose `event_id` is one of our truncates (race with the item end) is recorded as a warning, not a call error. Audio is `audio/pcmu` both ways (no resampling) under C0/C1; under W the adapter sends `audio/pcm` 24 kHz. `reasoning_effort` is not sent unless set, because the field shape for gpt-realtime-2.1 is not confirmed.
