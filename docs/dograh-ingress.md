# Dograh ingress and tool path (M0-T05)

Checked 2026-11-04, read-only. Source: `C:\Users\Lenovo\dograh` at commit `cb7f614a1d2fbcce3383a6e6720b56f6d8be7f32`
(branch `feat/call-analysis-logging`; the telephony and workflow files cited below also exist on `upstream/main`;
`ws_auth.py` landed upstream in #599 / #610). Pipecat submodule `b35b8e9761ecb2a28acff428544dbe246a95e180`.
Paths are relative to `dograh/api/` unless prefixed. Nothing was executed.

**Verdict: FALLBACK B is NOT required.** Run creation (1) and the tool path (4) can both be scripted through
Dograh's REST API with no carrier involved. Two one-time set-up preconditions apply: template-context variables on
the workflow, and a Twilio telephony-configuration row with placeholder credentials.

---

## (1) Creating a `twilio`-mode run left in `initialized`, with no carrier webhook

**Answer.** `POST /api/v1/workflow/{workflow_id}/runs` with body `{"mode": "twilio", "name": "<any>"}`.
It only inserts a row and never contacts Twilio; the row's `state` defaults to `initialized`.
The telephony WebSocket handler then requires `initial_context["provider"] == "twilio"`. This route does not take
`initial_context` from the request; it copies the workflow definition's **template-context variables** into
`initial_context`. So the workflow under test must carry
`template_context_variables = {"provider": "twilio", "telephony_configuration_id": <id>}` (set once with
`PUT /api/v1/workflow/{id}`, then publish), and the org must have a Twilio telephony configuration.
Placeholder `account_sid` / `auth_token` are accepted: nothing validates them when saved.

Evidence:
- `routes/workflow.py:314-316` — `class CreateWorkflowRunRequest(BaseModel): mode: str / name: str` (no initial_context field).
- `routes/workflow.py:1331` — `@router.post("/{workflow_id}/runs")`; `:1351-1356` — `prepare_workflow_run_inputs(db_client, workflow, use_draft=True, include_template_context=True)`; `:1376` — `run = await db_client.create_workflow_run(request.name, workflow_id, request.mode, ...)`. Response model `:319-326` returns `id, workflow_id, name, mode, created_at, definition_id, initial_context`.
- `services/workflow/run_creation.py:62-72` — with `include_template_context`, `default_context` = the target definition's `template_context_variables` (draft if one exists, `:36-40`); `:76-79` — `initial_context={**default_context, **(initial_context or {})}`.
- `db/models.py:624-628` — `state = Column(... default=WorkflowRunState.INITIALIZED.value, server_default=text("'initialized'::workflow_run_state"))`.
- `routes/workflow.py:288-292` — `UpdateWorkflowRequest.template_context_variables: dict | None`; `:1005` `@router.put("/{workflow_id}")` saves a new draft (`:1233-1237`). Do this once at set-up, not per call (it creates a version).
- `routes/telephony.py:701` — `if workflow_run.state != WorkflowRunState.INITIALIZED.value:` → close 4409.
- `routes/telephony.py:718` — `provider_type = workflow_run.initial_context.get("provider")`; `:740-746` close 4400 "No provider type found" when missing.
- `routes/telephony.py:757` — `provider = await get_telephony_provider_for_run(workflow_run, workflow.organization_id)`; `services/telephony/factory.py:176-179` — uses `initial_context.telephony_configuration_id`, else the org default config. `routes/telephony.py:764-766` — close 4400 "Provider mismatch" unless the config is Twilio.
- `routes/telephony.py:770-771` — sets state `running`, then `:779` `provider.handle_websocket(...)`.
- `services/telephony/providers/twilio/transport.py:31-41` — loads `account_sid` / `auth_token` from that config; `ValueError("Incomplete Twilio configuration ...")` if empty, so placeholders must be present.
- `routes/organization.py:808-826` — `POST /api/v1/organizations/telephony-configs` stores credentials. Twilio registers no `preprocess_credentials_on_save` (only ari, cloudonix, plivo, telnyx, vobiz do: `services/telephony/providers/*/__init__.py`), so no Twilio API call on save.
- `services/telephony/providers/twilio/config.py:8-13` — `TwilioConfigurationRequest{provider:"twilio", account_sid, auth_token}`.

Routes that are **not** usable:
- `POST /api/v1/telephony/initiate-call` (`routes/telephony.py:85-300`) creates the run correctly (`:189-216`, sets `provider` and `telephony_configuration_id`), then calls `provider.initiate_call` (`:275`), which POSTs to `https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json` (`services/telephony/providers/twilio/provider.py:59-118`). That is a real, paid outbound call. Excluded.
- `/api/v1/agent-stream/{provider_name}/{workflow_uuid}` (`routes/agent_stream.py:33-144`) creates a run and dispatches to `handle_external_websocket`, which only Cloudonix implements (`services/telephony/providers/cloudonix/provider.py:581`). Twilio inherits the base stub (`services/telephony/base.py:442`) → `NotImplementedError` → close 1011 (`agent_stream.py:129-134`).

Differences from a production call (disclose in the run card): `POST /runs` takes no concurrency slot and runs no
quota check (contrast `routes/telephony.py:181` and `:243`); it uses the draft definition if one exists
(`use_draft=True`, `routes/workflow.py:1354`), so publish and leave no draft, and record `definition_id`;
`call_type` is `outbound` for non-WebRTC modes (`:1359`).

Script (set-up once, then per call):
1. `POST /api/v1/organizations/telephony-configs` `{"name":"duplexbench","config":{"provider":"twilio","account_sid":"AC<placeholder>","auth_token":"<placeholder>"}}` → `config_id`.
2. `PUT /api/v1/workflow/{wf}` `{"template_context_variables":{"provider":"twilio","telephony_configuration_id":<config_id>}}`; then `POST /api/v1/workflow/{wf}/publish` (`routes/workflow.py:803`).
3. Per call: `POST /api/v1/workflow/{wf}/runs` `{"mode":"twilio","name":"DB-<call_token>"}` → `run_id`, `definition_id`; assert `initial_context.provider == "twilio"`.
4. Open `ws(s)://<dograh>/api/v1/telephony/ws/{wf}/{org_id}/{run_id}[/{token}]` (see 2 and 3).

REST auth: header `X-API-Key: <key>` (`services/auth/depends.py:33-41`); keys from `POST /api/v1/user/api-keys`
(`routes/user.py:381`). API prefix `/api/v1` (`app.py:51`, `:168`).

## (2) First WebSocket messages Dograh's Twilio handler expects

**Answer.** Exactly two text frames, in order, before any audio: `{"event":"connected", ...}`, then
`{"event":"start","start":{"streamSid":..., "callSid":..., ...}}`. Only `start.streamSid` and `start.callSid` are read.
**`customParameters` is not read anywhere** in the Twilio handler. `callSid` becomes `call_id` in the workflow's call
context, so the harness can put its `call_token` there (see 4).
After the handshake, pipecat's `TwilioFrameSerializer` handles traffic. Inbound: `media` (base64 μ-law 8 kHz) and
`dtmf`; everything else inbound (`mark`, `stop`, …) is ignored. Outbound: `media` and `clear` only. **Dograh never
sends `mark`**, so mark-based playout tracking is unavailable on this path.

Evidence:
- `services/telephony/providers/twilio/provider.py:270-275` — `first_msg = await websocket.receive_text()` … `if msg.get("event") != "connected":` → close 4400 "Expected connected event".
- `provider.py:282-289` — second frame; `if start_msg.get("event") != "start":` → close 4400 "Expected start event".
- `provider.py:291-297` — `stream_sid = start_msg["start"]["streamSid"]` / `call_sid = start_msg["start"]["callSid"]`; `KeyError` → close 4400 "Missing stream identifiers".
- `provider.py:299-307` — `run_pipeline_telephony(..., call_id=call_sid, transport_kwargs={"stream_sid": stream_sid, "call_sid": call_sid})`.
- `services/pipecat/run_pipeline.py:451` — `provider_call_id=call_id`; `:674` — `merged_call_context_vars["call_id"] = provider_call_id`; `:668` — `merged_call_context_vars["workflow_run_id"] = workflow_run_id`.
- `services/telephony/providers/twilio/__init__.py:59` — `transport_sample_rate=8000`.
- `dograh/pipecat/src/pipecat/serializers/twilio.py:330-344` — inbound `media` → `ulaw_to_pcm(payload, 8000, pipeline_rate)`; `:346-353` `dtmf`; `:354-355` `else: return None`.
- `serializers/twilio.py:227-229` — `InterruptionFrame` → `{"event": "clear", "streamSid": ...}` (map to harness `cancel_or_truncate`).
- `serializers/twilio.py:230-247` — audio → `{"event":"media","streamSid":...,"media":{"payload":<b64 μ-law 8 kHz>}}` (no `timestamp`, `chunk` or `track`; frame size is whatever pipecat's output transport emits).
- `serializers/twilio.py:189-225` — on `EndFrame` / `CancelFrame` with `auto_hang_up` (default `True`, `:85`) it runs `TwilioHangupStrategy` (`services/telephony/providers/twilio/strategies.py:142-200`), which POSTs to `api.twilio.com/.../Calls/{call_sid}.json`. With placeholder credentials and a fake `callSid` this fails (401/404) and is logged. It is a request to Twilio at call end, not a billed call; record it as a known side effect.

Minimal harness frames (Dograh ignores extra fields; send Twilio's full documented shape so one codec serves the
phone path too):
```json
{"event":"connected","protocol":"Call","version":"1.0.0"}
{"event":"start","sequenceNumber":"1","streamSid":"MZ<id>","start":{"streamSid":"MZ<id>","callSid":"CA<call_token>","accountSid":"AC<placeholder>","tracks":["inbound"],"mediaFormat":{"encoding":"audio/x-mulaw","sampleRate":8000,"channels":1},"customParameters":{}}}
{"event":"media","sequenceNumber":"2","streamSid":"MZ<id>","media":{"track":"inbound","chunk":"1","timestamp":"0","payload":"<b64 160 bytes>"}}
```

## (3) Capability token on the WebSocket path

**Answer.** Optional, controlled by env. With `TELEPHONY_WS_TOKEN_SECRET` unset (the default) no token is minted or
checked and the 3-segment URL works. With it set, the token is
`hex(HMAC-SHA256(secret, f"{workflow_id}:{organization_id}:{workflow_run_id}"))`, sent as a 4th path segment (or
`?token=`). With `TELEPHONY_WS_TOKEN_ENFORCE=true` a bad or missing token closes 4401; otherwise it is logged and
allowed. The token is stateless, and no API returns it for a run created by `POST /runs`, so the harness mints it
itself when given the same secret.

Evidence:
- `constants.py:148-150` — `TELEPHONY_WS_TOKEN_SECRET = os.getenv("TELEPHONY_WS_TOKEN_SECRET") or None`; `TELEPHONY_WS_TOKEN_ENFORCE = (os.getenv("TELEPHONY_WS_TOKEN_ENFORCE", "false").lower() == "true")`.
- `services/telephony/ws_auth.py:49-61` — `_canonical` returns `f"{workflow_id}:{organization_id}:{workflow_run_id}"`; `mint_ws_token` returns `hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()`, or `None` without a secret.
- `ws_auth.py:114-122` — URL `{wss_base}/api/v1/telephony/ws/{wf}/{org}/{run}` plus `/{token}` when minted.
- `routes/telephony.py:600-601` — both `/ws/{workflow_id}/{organization_id}/{workflow_run_id}/{token}` and the 3-segment form.
- `routes/telephony.py:653-671` — `if ws_auth.token_configured(): presented = token or websocket.query_params.get("token")`; close 4401 only `if ws_auth.enforcement_enabled()`.
- `routes/telephony.py:639-645` — TODO(security): the token is replayable while the run is `initialized`.

## (4) How a workflow calls an external HTTP tool; base URL per workflow

**Answer.** Tools are org-level rows created by `POST /api/v1/tools/` with `definition.type = "http_api"` and
`definition.config = {method, url, headers?, credential_uuid?, parameters[], preset_parameters[], timeout_ms (default 5000)}`.
A workflow node lists tools in `data.tool_uuids`. When the LLM calls the function, `execute_http_tool` renders `{{...}}`
placeholders in `config.url` from a context of LLM args + `initial_context` (template vars) + `gathered_context`,
which includes `workflow_run_id` and `call_id` (= the `callSid` the harness sent). It then makes one `httpx`
request: JSON body for POST/PUT/PATCH (the LLM args, or `body_template`), query params for GET/DELETE.
**There is no per-workflow tool base URL setting; the URL belongs to the tool.** For per-call routing, set the tool
URL to `http://<harness-host>:<port>/tools/{{call_id}}/<tool_name>` and send `callSid = <call_token>` in `start`
(alternative: `{{workflow_run_id}}`, which the harness learns from `POST /runs`). A host may come from a template
variable, but values are percent-encoded with `safe=""`, so a `host:port` value breaks: keep the port literal.
In `DEPLOYMENT_MODE=oss` (default) private and localhost URLs are allowed; SaaS mode rejects them.

Evidence:
- `routes/tool.py:48` — `APIRouter(prefix="/tools")`; `:137-160` — `POST /` → `create_tool_for_user`.
- `schemas/tool.py:90-133` — `HttpApiConfig{method, url, headers, credential_uuid, parameters, preset_parameters, timeout_ms=5000, customMessage, customMessageType}`; `:602-630` — `CreateToolRequest{name, description, category, icon, definition}`.
- `services/workflow/dto.py:165` — node field `tool_uuids: Optional[List[str]]`.
- `services/workflow/pipecat_engine_custom_tools.py:430-436` — `execute_http_tool(tool=tool, arguments=function_call_params.arguments, call_context_vars=self._engine._call_context_vars, gathered_context_vars=self._engine._gathered_context, organization_id=...)`; `:403-417` — an optional `customMessage` is spoken before the request (record it in the system config; it affects `T_tool_to_info_ear`); `:420-427` — doctor dry-run short-circuit (not active in normal runs).
- `services/workflow/tools/custom_tool.py:306-307` — `method = config.get("method", "POST").upper()` / `url = config.get("url", "")`; `:372` — `timeout_ms = config.get("timeout_ms", 5000)`; `:407-421` — render context and `render_url_template`; `:429` — `validate_user_configured_service_url`; `:439-454` — body or params; `:465-470` — `httpx.AsyncClient(timeout=timeout_seconds)` … `client.request(method=method, url=url, headers=headers, json=body, ...)`; `:504-521` — timeout → `{"status":"error","error":"Request timed out after N seconds"}`. The harness's injected tool latency must stay under `timeout_ms`, or `timeout_ms` is set per tool and recorded.
- `services/pipecat/run_pipeline.py:668`, `:674` — `workflow_run_id` and `call_id` in the call context.
- `utils/template_renderer.py:265-304` — URL template; `:304` — `return quote(rendered_value, safe="")`.
- `utils/url_security.py:21-22` — `if DEPLOYMENT_MODE == "oss": return`; `constants.py:66` — `DEPLOYMENT_MODE = os.getenv("DEPLOYMENT_MODE", "oss")`.
- Other HTTP paths, not needed by the harness: `services/pipecat/pre_call_fetch.py` (pre-call data fetch), `services/workflow/mcp_tool_session.py` (MCP tools).

## (5) Reading the workflow's id, version and config hash

**Answer.** Dograh stores **no live config hash**: `workflow_definitions.workflow_hash` is marked "Legacy, no longer
used". The harness computes its own. Per run, read `definition_id` from the `POST /runs` response (the pinned
definition); `GET /api/v1/workflow/{wf}/versions` gives that definition's `version_number`, `status`,
`workflow_json`, `workflow_configurations`, `template_context_variables`; `GET /api/v1/workflow/fetch/{wf}` gives
`workflow_uuid`, `current_definition_id`, `version_number`. Hash = `canonical_json` of the pinned definition's
(`workflow_json`, `workflow_configurations`, `template_context_variables`) plus the effective org model
configuration. STT/LLM/TTS are resolved at run time from org config + `workflow_configurations`
(`services/pipecat/run_pipeline.py:414-422`, `get_effective_ai_model_configuration_for_workflow`); that org-level
part is not in the definition and must be captured separately. Both responses mask secrets
(`mask_workflow_definition`, `mask_workflow_configurations`); fine for hashing if masking is deterministic (verify at M6-T02).

Evidence:
- `db/models.py:440-470` — `WorkflowDefinitionModel`: `workflow_hash = Column(String, nullable=True)  # Legacy, no longer used`; `version_number` "Sequential per workflow, display only"; `workflow_configurations`, `template_context_variables` under "Full behavioral snapshot".
- `db/models.py:610` — `WorkflowRunModel.definition_id`.
- `routes/workflow.py:319-326` — run-create response includes `definition_id`.
- `routes/workflow.py:762-800` — `GET /{workflow_id}/versions` → `WorkflowVersionResponse{id, version_number, status, created_at, published_at, workflow_json, workflow_configurations, template_context_variables}`.
- `routes/workflow.py:708-760` — `GET /fetch/{workflow_id}` → `id, name, status, workflow_definition, current_definition_id, template_context_variables, workflow_configurations, version_number, version_status, workflow_uuid`.
- `routes/workflow.py:1399` — `GET /{workflow_id}/runs/{run_id}` for post-call run state.
- `services/pipecat/run_pipeline.py:682-684` — the pipeline uses the run's pinned `definition.workflow_json` / `workflow_configurations`.

## Alternative ingress: SmallWebRTC signaling

`routes/webrtc_signaling.py:836-871` — `@router.websocket("/signaling/{workflow_id}/{workflow_run_id}")` under prefix
`/ws` (`:62`), i.e. `/api/v1/ws/signaling/{wf}/{run}`. Auth by `?api_key=` or `?token=` query (`get_user_ws`,
`services/auth/depends.py:342-361`). Messages are JSON with `type` `"offer"` or `"ice-candidate"` (`:494-510`); the
answer comes back on the socket and media flows over WebRTC (`run_pipeline_smallwebrtc`). The run is created with
`POST /runs` `mode:"smallwebrtc"`; the telephony WS rejects such runs (`routes/telephony.py:723-736`). WebRTC modes
run at 16 kHz (`services/pipecat/audio_config.py:93`, `:98`) with Opus on the wire, so this path does not exercise
8 kHz μ-law. The harness would need `aiortc` (RTCPeerConnection client, ICE/TURN) — heavier, fallback only. This
path does reserve a concurrency slot (`enforce_call_concurrency=True`, `:869`).

## Open items for M6-T02
- Confirm on a running Dograh instance (no carrier) that steps 1-4 reach `run_pipeline_telephony` and audio flows. This document is static reading only.
- Choose the `call_token` carrier: `callSid` (harness-chosen; recommended) or `workflow_run_id`.
- The end-of-call `api.twilio.com` hangup attempt (fails on placeholders): record in the run card, or ask Dograh whether `auto_hang_up` can be disabled. No config hook found (`twilio/transport.py:43-50` passes no `params`).
- Run-card disclosure: the `POST /runs` path skips the concurrency slot and quota check.
- Dograh sends no `mark`; playout timing at the harness must come from received `media` frames, not marks.
