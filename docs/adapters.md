# Adapters (M6): harness event ← provider event

Written 2026-11-05 from `docs/providers.md` (checked 2026-11-04), `docs/dograh-ingress.md` and
`docs/reference-notes.md`. No provider was called. "confirmed" = the docs (or the read source) state
the event and field; "UNCONFIRMED" = documented best guess, marked `# UNCONFIRMED per docs/providers.md`
in code; the M6 live smoke must fix or confirm each one before results count.

Common rules (all adapters, `adapters/ws_adapter.py` on `transport/live_stream.py`; `adapters/_convert.py`):
- `first_audio_byte` is never emitted by an adapter; the base class emits it before the first `audio`
  after each `call.turn_ended(t)`.
- Socket closed after the harness's `close()` → `closed`. Provider closes cleanly (1000/1001) →
  `closed {"by":"provider"}`. Abnormal close / reset → `error {"type":"transport"}` then `closed`.
  Sends on a dead socket are swallowed so the drop is reported once, by the receive loop.
- Input: the runner delivers μ-law 8 kHz only when the condition has a codec and `caps.native_mulaw`;
  otherwise PCM16 at the line rate. `InputConverter` re-encodes / resamples (`channel.resample.Resampler`).
- Unmapped provider messages go to `adapter.raw_log` (t_ns, name); they are not invented into events.

## dograh_stack (`adapters/dograh_stack.py`, D2 harness-as-carrier)

| Harness event | Dograh / Twilio event | Status |
|---|---|---|
| run creation | `POST /api/v1/workflow/{wf}/runs` `{"mode":"twilio","name":"DB-<token>"}`, `X-API-Key` | confirmed (source read) |
| pin + hash | `GET /api/v1/workflow/fetch/{wf}` → `current_definition_id`; `GET /{wf}/versions` → version; sha256 of canonical (`workflow_json`, `workflow_configurations`, `template_context_variables`, `org_model_config`, and `tool_definitions` when fetched). fix-35: `GET /api/v1/tools/` rows referenced by any `tool_uuids` in the graph (name, description, category, status, definition; header names only; credential as a presence flag). fix-36: `model_source` = `workflow_override` (v2 override set; nothing fetched) / `org_config` (`GET /api/v1/user/configurations/user`, secrets, test phone, pricing dropped) / `pasted` (YAML fallback after a failed fetch) / `unknown`; failures become run-card disclosures | confirmed routes (Dograh source); `/versions` list-vs-object shape UNCONFIRMED; hosted app 404s on the user-configurations route (G11) |
| WS URL | `ws(s)://<host>/api/v1/telephony/ws/{wf}/{org}/{run}[/{hmac token}]` | confirmed |
| handshake | `connected`, then `start{streamSid, callSid=<call tool token>}` | confirmed |
| session_open | harness, after the handshake | harness rule |
| audio | `media` (b64 μ-law 8 kHz) | confirmed |
| cancel_or_truncate | `clear` | confirmed |
| (log only) | `mark` | Dograh never sends it |
| tool_call | none on the socket; Dograh http_api tool → `POST http://<harness>/tools/{{call_id}}/<tool>` → ToolServer; adapter logs via `tool_events` (= `ToolServer.subscribe`) | confirmed path; never re-emitted (runner would double-invoke) |
| output_transcript, usage | not on the Twilio wire | absent |

Config keys (`params`): `endpoint` (REST base, required), `ws_endpoint` (optional WS host
override), `workflow_id`, `org_id` (required), `api_key_env` (default `DOGRAH_API_KEY`),
`ws_token_secret_env` (optional), `run_name_prefix` (default `DB-`), `harness_tool_base`,
`org_model_config` (optional pasted fallback, fix-36), `custom_parameters`, `headers`.

Run card (`run_card_fields()`): workflow_id, org_id, pinned definition_id, run definition_id,
`definition_matches_pinned`, config_hash and its scope, `model_source`, `model_names` (provider/model only),
`tool_definitions_hashed`, `tool_uuids_referenced`, ws_token used, disclosures (no concurrency
slot / quota check; draft definition used if present; Twilio hang-up side effect; no `mark`;
caller muted during every tool call and, on the realtime path, until the first bot utterance completes;
plus any pin-time fallback such as "tool definitions not hashed: <reason>").

Live smoke checklist (M6-T02):
1. Set-up per dograh-ingress.md: Twilio telephony config with placeholders; workflow
   `template_context_variables = {provider: twilio, telephony_configuration_id}`; publish, leave no draft.
2. Create the org-level http_api tools with `tool_url_template(harness_base, name)`; keep the port literal.
3. `POST /runs` returns `initial_context.provider == "twilio"` and a `definition_id` equal to the pinned one.
4. The WS reaches `run_pipeline_telephony`; agent `media` frames arrive; record their size (frame ms).
5. Barge-in produces `clear`; check `clear` timing vs the last media frame.
6. A tool call reaches `/tools/<token>/<tool>`; `tool_calls` row present; `customMessage` (if any) recorded.
7. `/versions` response shape; whether masking is deterministic (hash stable across two pins).
8. Run card `model_source` is `workflow_override` (B21) or `org_config`; `unknown`/`pasted` means the user-configurations route failed.
9. With `TELEPHONY_WS_TOKEN_ENFORCE=true`, the minted token is accepted.
10. The end-of-call api.twilio.com hang-up attempt: confirm it fails harmlessly; note in the run card.

## pipecat_cascade (`adapters/pipecat_cascade.py`)

Same Twilio-protocol mapping as Dograh (shared `TwilioAgentAdapter`): `media` → audio (confirmed,
serializer source), `clear` → cancel_or_truncate (confirmed), `mark` logged, tools via the harness
ToolServer over HTTP. `customParameters` carries `call_token` (Dograh ignores it; a custom pipeline may read it).
Config: `endpoint` (required), `components.{stt,llm,tts}` (required, each with vendor/model/version),
`barge_in`, `filler_policy`, `tool_url_template`.
Live smoke: vendors chosen (human); pipeline's handshake parser accepts our `connected`+`start`;
`InterruptionFrame` → `clear` observed; endpointing params recorded.

## gemini_live (`adapters/gemini_live.py`)

| Harness event | Gemini message | Status |
|---|---|---|
| setup | `{"setup":{model:"models/<id>", generationConfig.responseModalities:["AUDIO"], realtimeInputConfig, tools[].functionDeclarations, outputAudioTranscription}}` | message confirmed; nesting of speech/voice config UNCONFIRMED |
| endpoint/auth | `wss://generativelanguage.googleapis.com/ws/...BidiGenerateContent?key=` | path confirmed; host + `key` query UNCONFIRMED |
| send audio | `realtimeInput.audio{data, mimeType:"audio/pcm;rate=16000"}` | UNCONFIRMED (`audio` vs `mediaChunks`) |
| session_open | `setupComplete` | confirmed |
| audio | `serverContent.modelTurn.parts[].inlineData` (PCM16 24 kHz) | confirmed |
| cancel_or_truncate | `serverContent.interrupted: true` | confirmed |
| output_transcript | `serverContent.outputTranscription.text` | message confirmed; `text` field UNCONFIRMED |
| tool_call | `toolCall.functionCalls[]{id,name,args}` | message confirmed; inner fields UNCONFIRMED |
| tool result | `toolResponse.functionResponses[]{id,name,response}` | UNCONFIRMED shape |
| usage | `usageMetadata` | confirmed |
| log only | `toolCallCancellation`, `goAway`, `turnComplete`, `generationComplete` | confirmed names |

Caps: no μ-law (PCM16 16 kHz in via Resampler, 24 kHz out). Live smoke: all UNCONFIRMED rows; GoAway
at ~10 min is beyond a call; record realtimeInputConfig defaults actually applied.

## grok_voice (`adapters/grok_voice.py`)

| Harness event | Grok event | Status |
|---|---|---|
| config | `session.update{session:{audio.input/output.format:{type:"audio/pcmu",rate:8000}, turn_detection, reasoning.effort, tools}}` | formats confirmed; `turn_detection` placement UNCONFIRMED |
| session_open | `session.updated` | UNCONFIRMED (which lifecycle event) |
| send audio | `input_audio_buffer.append{audio}` | confirmed (Realtime shape) |
| audio | `response.output_audio.delta` / `response.audio.delta` | confirmed |
| cancel_or_truncate | `input_audio_buffer.speech_started` **while a response is producing audio** | event confirmed; the "while producing" gate is a harness rule (a speech_started with nothing playing is logged only) |
| output_transcript | `response.output_audio_transcript.done{transcript}` | confirmed |
| tool_call | `response.function_call_arguments.done{call_id,name,arguments}` | confirmed |
| tool result | `conversation.item.create{function_call_output}` + `response.create` | confirmed |
| usage | `response.done.response.usage` | UNCONFIRMED (none documented; cost is per minute) |

No `conversation.item.truncate` is sent. Live smoke: lifecycle event, turn_detection placement,
whether `response.done` carries a cancelled status and usage.

## gpt_live (`adapters/gpt_live.py`)

| Harness event | GPT-Live event | Status |
|---|---|---|
| start | `session.start{session:{model, audio.format:{type:"audio/pcmu",rate:8000}, delegation.type}}` | event + format confirmed; payload nesting and `model` placement (also `?model=` in URL) UNCONFIRMED |
| session_open | `session.started` | confirmed name |
| send audio | `session.input_audio.append{audio}` | confirmed |
| audio | `session.output_audio.delta{delta}` | confirmed |
| cancel_or_truncate | **none** | documented absence → `caps.cancel_event=False`, YAML `cancel_event: false`, report "cancel" caveat |
| output_transcript | `session.output_transcript.delta{delta}` (deltas, no turn end) | confirmed |
| tool_call | `session.delegation.created{delegation:{id,...}}` | name confirmed; `name`/`arguments` fields UNCONFIRMED |
| tool result | `session.thinking.append{delegation_id,text}` | UNCONFIRMED shape |
| usage | `session.usage.updated{usage.seconds}` (cumulative), final in `session.closed` | confirmed |
| closed | `session.closed` | confirmed |

Tools in `session.start` are UNCONFIRMED. Live smoke: start payload, delegation payload and reply,
Build tier access.

## selfhosted_fd (`adapters/selfhosted_fd.py`)

Harness-defined protocol for a shim in front of Moshi / PersonaPlex (their own protocol is Opus 24 kHz
with a type byte, providers.md §2): `start` JSON, binary PCM16 LE both ways, `ready` → session_open,
`transcript` → output_transcript, `cancel` → cancel_or_truncate only when the config's
`cancel_event: true` (Moshi: false; logged, not mapped), `error` → error. `self_hosted=True`. No tools.
Live smoke: GPU approval; the shim does not exist yet (needs writing on the GPU box).
