# Workflow spec: DuplexBench Support Line

Status: build specification. Written 2026-11-06 from a static reading of the harness
(`C:\Users\Lenovo\research\duplex-bench`) and of the Dograh source (`C:\Users\Lenovo\dograh`, read only, nothing run).
Dograh paths are relative to `dograh/api/` unless the path starts with `dograh/`.
Harness paths are relative to the harness repo root.
Updated 2026-11-08 with the hosted build (section 1.1, decision B23). Section 1.1 governs where it and a later section differ.

Who uses this document: the person who builds the Dograh workflow for the "Dograh stack" system
(`configs/systems/dograh_stack.yaml`). This person uses the Dograh MCP server, the Dograh REST API, or the Dograh UI.

What this document does not do: it does not change the harness. It does not tune the system.
Where the harness and Dograh do not fit together, section 9 lists the gap. A human decides each gap.

---

## 1. Purpose and the ten standards

The harness calls one published Dograh workflow as if the harness were Twilio (docs/dograh-ingress.md).
The workflow must answer callers in all five harness domains. It must call the harness mock tools.
Its behaviour must be the behaviour that the metrics of SPEC §4.1 to §4.3 reward, with no trick that games a metric.

The ten standards come from docs/TODO.md §1 "Workflow choice". The workflow must meet all ten.

| # | Standard (TODO §1) | Dograh setting that satisfies it |
|---|---|---|
| 1 | One named full-duplex model per Dograh system under test (decision B21). The run card records `definition_id` and a hash of the definition. That hash includes the model override. | On the hosted app, set `model_configuration_override` (model config v3, decision B23) in `workflow_configurations`. It names one provider connection and one realtime model (section 5.4.1). Only this workflow uses the realtime model. The org default stays the cascade. A self-hosted Dograh at the source version uses `model_configuration_v2_override` instead (section 5.4.2). Use one workflow, or one override value, per model under test. Then each Dograh system has its own `config_hash`. Section 5 lists every other setting. |
| 2 | English (US) only. | All prompts and the greeting are in US English (section 3). Record the language of the realtime override (section 5.4). |
| 3 | The workflow handles every domain the pack uses. | One `startCall` router plus one `agentNode` per domain: scheduling, orders, billing, account, outbound (section 2). |
| 4 | Every tool exists as an org-level HTTP tool with the harness name. POST. URL `http://<harness-host>:<port>/tools/{{call_id}}/<tool_name>`. Body is the LLM arguments as JSON. Literal port. | 21 tools, `definition.type` `http_api`, `config.method` `POST`, `config.url` as stated, no `body_template` (so the body is the flat argument object: `services/workflow/tools/custom_tool.py:439-442`). Section 4. |
| 5 | `timeout_ms` larger than the largest injected latency. Recorded. | `config.timeout_ms` = 15000 on every tool (`schemas/tool.py:131-135`, default 5000). Section 4.1 explains the value. |
| 6 | No `customMessage`. | `config.customMessage` absent (null) and `config.customMessageType` absent on every tool (`schemas/tool.py:136-144`). |
| 7 | Template variables `provider: twilio` and `telephony_configuration_id`. Published. No draft left. | `template_context_variables` = `{"provider": "twilio", "telephony_configuration_id": <id>}` through `PUT /api/v1/workflow/{id}` (`routes/workflow.py:289-296`, `:1005`), then `POST /api/v1/workflow/{id}/publish` (`routes/workflow.py:803-854`). Section 5 and 8. |
| 8 | Who speaks first is known and matches `simulator.caller_first`. | The agent speaks first. The `startCall` node has `greeting_type` `text` and a `greeting`. With no greeting, Dograh asks the model for an opening turn (`services/pipecat/event_handlers.py:198`, `generate_if_no_greeting=True`), so the agent speaks first in every case. Set `simulator.caller_first: false` in the run config. Section 6. |
| 9 | Identity is verified before an account change. | Node n5 Account calls `verify_identity` (customer_id, dob, zip) and calls no write tool before `verified: true`. The account mock always refuses an unverified write with `PolicyError("auth_required")` (`src/duplexbench/tools/mock_backends/account.py`). Node n4 Billing also calls its own `verify_identity` (customer_id = billing account ID, dob, zip) before every payment or refund. The billing mock refuses an unverified `make_payment` or `issue_refund` only when the scenario sets `policy.require_identity: true` (`src/duplexbench/tools/mock_backends/billing.py`). The agent cannot know the scenario policy, so P-BILLING always checks identity first. Sections 3.5 and 3.6. |
| 10 | No live side effect. | Every tool URL points at the harness. No SMS, email, payment or transfer tool. No `webhook`, `qa` or `trigger` node. `pre_call_fetch_mode` `disabled`. One known side effect stays: the Twilio hang-up request at call end (dograh-ingress.md (2)). |

### 1.1 As built (2026-11-08, hosted)

The workflow session built this workflow on the hosted Dograh app and read it back live on 2026-11-08.
Where this section and a later section differ, this section describes the hosted build. The later sections describe the source-version design.

| Item | Value |
|---|---|
| API base | `https://app.dograh.com/api/v1` (hosted SaaS). Auth header `X-API-Key`, value from env `DOGRAH_API_KEY`. The key never goes into this document. |
| Org | `org_id` 6993 |
| Workflow | `workflow_id` 14735, name `DuplexBench Support Line` |
| Agent uuid | `fb1bce37-a195-4fb0-8309-521087899c00` |
| Published version | version 3, `definition_id` 42706. Versions 1 and 2 have status archived. No draft exists. |
| Telephony configuration | `telephony_configuration_id` 5767. Placeholder Twilio credentials, `account_sid` `AC000...`, `is_default_outbound` false. |
| Template context variables | `{"provider": "twilio", "telephony_configuration_id": 5767}` |
| Model | key `model_configuration_override` (model config v3). Provider connection "full duplex bench" (`64141402-aa1a-4fbe-9aa9-c70f18ca5f66`), provider `openai_realtime`, model `gpt-realtime-2.1`, voice `alloy`, language `en`. Section 5.4.1. |
| Who speaks first | The agent, with greeting G-1 (section 6). |

Nodes. The stored ids equal the spec ids of section 2.4. The prompts are the verbatim prompts of section 3.

| Stored id | Spec id | `type` | Role |
|---|---|---|---|
| 1 | n1 | `startCall` | router |
| 2 | n2 | `agentNode` | Scheduling |
| 3 | n3 | `agentNode` | Orders |
| 4 | n4 | `agentNode` | Billing |
| 5 | n5 | `agentNode` | Account |
| 6 | n6 | `agentNode` | Outbound |
| 7 | n7 | `globalNode` | Line Rules |
| 8 | n8 | `endCall` | end of call |

Edges: `1-2` to `1-6` are e1 to e5. `1-8` "Early Goodbye" is e6. `2-8` to `6-8` "Finish Call" are e7 to e11. This is the graph of sections 2.3 and 2.6.
Nodes 1 to 6 have `allow_interrupt: true`. A UI draft added `allow_interrupt: false` on node 7 and node 8. These node types have no such runtime field (section 2.5), so the value has no effect.

Tools. All 21 rows have `http_api`, method `POST`, `timeout_ms` 15000 and `messages` `[]`. No row has headers, a credential or a `body_template`.
Every URL is `http://HARNESS_HOST:8900/tools/{{call_id}}/<tool_name>`. `HARNESS_HOST` is a placeholder, not a host. No call can reach the harness until it changes.
When the tunnel hostname exists, the workflow session updates the 21 URLs and records the new pattern here (gap G13).

| Tool | Node | `tool_uuid` |
|---|---|---|
| `find_slots` | 2 Scheduling | `f7589b96-4b8c-4197-aa47-04c995adbdc1` |
| `book_appointment` | 2 Scheduling | `9c6d1d30-fcff-4d85-a9f4-880eff551e31` |
| `reschedule_appointment` | 2 Scheduling | `5c2cfd2a-406b-451b-9b32-d33f7b7e1ea8` |
| `cancel_appointment` | 2 Scheduling | `2c0e58a2-fe3a-4770-b173-0c141809cdf9` |
| `lookup_order` | 3 Orders | `56be335f-2b6d-4726-9f16-f19f083adc2b` |
| `cancel_order` | 3 Orders | `34feb2e1-3338-4bf5-8e98-28562d3b1af4` |
| `update_shipping_address` | 3 Orders | `e59edc7a-3c0f-4c68-a7ad-7be8009ad78b` |
| `create_return` | 3 Orders | `33d11026-0271-43da-b82e-bf97b06725c6` |
| `verify_identity` (billing row) | 4 Billing | `3a12b22f-a9d8-4ad9-844a-daabe07eacbd` |
| `get_balance` | 4 Billing | `48180828-175f-43b6-8d21-b99f56ee932b` |
| `make_payment` | 4 Billing | `1e75915a-5811-4e66-878c-e35342622d85` |
| `issue_refund` | 4 Billing | `568e5fbf-f832-4029-9cba-108f39e596d4` |
| `verify_identity` (account row) | 5 Account | `6b06f3ab-1029-41ac-a61a-16bf0a1be084` |
| `get_account` | 5 Account | `052d24ec-0046-43a6-a05b-361707f99dd5` |
| `update_phone` | 5 Account | `e0008839-80c7-460b-a33f-5424fd07993d` |
| `update_email` | 5 Account | `824a4138-6a92-458e-b002-06c863ce9b3b` |
| `update_address` | 5 Account | `ba308ffc-f48a-4778-bd48-d505403842d0` |
| `get_contact` | 6 Outbound | `5f6decbe-9ee8-4263-a599-3d71ac17cc82` |
| `record_confirmation` | 6 Outbound | `acb95690-39f5-4dd8-9f4d-49a4cbd332ac` |
| `schedule_callback` | 6 Outbound | `7435a554-814a-47c7-8a08-9b6f7440a614` |
| `mark_do_not_call` | 6 Outbound | `0fe131d8-cfdc-47b4-9cca-2fd954394e6a` |

Published `workflow_configurations` of version 3, beside the values of section 5.3:

| Key | Hosted value | Section 5.3 value | Difference |
|---|---|---|---|
| `max_call_duration` | `600` | `600` (decision B18) | none |
| `max_user_idle_timeout` | `10.0` | production value, `10.0` if production has none | none |
| `ambient_noise_configuration` | off | `{"enabled": false, "volume": 0.3}` | none |
| `context_compaction_enabled` | `false` | production value, default `false` | none |
| `tts_cache_enabled` | `false` | production value, default `false` | none |
| `dictionary` | `""` | production value, default `""` | none |
| `call_dispositions` | `[]` | `[]` | none |
| `turn_start_strategy` | `"min_words"` | production value, Dograh default `"default"` | differs. The hosted value is the hosted default. |
| `turn_start_min_words` | `2` | production value, Dograh default `3` | differs. The hosted value is the hosted default. |
| `turn_stop_strategy` | `"transcription"` | production value, default `"transcription"` | none |
| `smart_turn_stop_secs` | `2.0` | production value, default `2.0` | none |

The four turn keys are cascade-only in the source (section 5.3). In realtime mode the provider takes the turns, so Dograh probably ignores these keys. Nobody has read the hosted code to prove it.
The definition hash still records the hosted values. Do not change them on a workflow that has results.

Harness consequence of "agent speaks first": a Track A or Track D run against this system sets `scripted: {lead_in: agent_first}` in the run config (decision B23).
Then scripted step 1 starts after the first agent end of turn (gap G1 option (a)). The run config also keeps `simulator.caller_first: false` (section 6).

Open items for the hosted build: gaps G13 to G16 in section 9.

---

## 2. Architecture

### 2.1 Decision

Build ONE workflow. Name: `DuplexBench Support Line`.
It serves the five harness domains through an intent router.

Why one workflow and not five:
The adapter reads one `params.workflow_id` from `configs/systems/dograh_stack.yaml`.
It creates every run with `POST /api/v1/workflow/{workflow_id}/runs` on that id (`src/duplexbench/adapters/dograh_stack.py`, `DograhClient.create_run`).
The run config has no field that maps a scenario domain to a workflow.
Five workflows would need four harness changes. The system YAML needs a `workflow_id_by_domain` map. The adapter needs a lookup by `scenario.domain`. The run card needs one definition hash per workflow. A rule must say how five hashes enter `config_id`.
That change is a schema decision (CLAUDE.md §6 item 8). One workflow needs no harness change.
The cost of one workflow: the first caller turn of every call passes through the router. The router runs on the realtime model, like every node. The transition is one function call before the first domain reply. It affects `T_turn_gap_ear` on the first turn only.

The router and all prompts run on the realtime speech-to-speech model of the override (section 5.4). The prompts in section 3 were written for an LLM that reads text. A speech-to-speech model receives them as its instructions.

### 2.2 Change from the requested shape: the router is the `startCall` node

The request named a separate identification and intent node after `startCall`.
This spec puts the intent router inside the `startCall` node. Reasons:

1. Dograh moves between nodes only through edge transition functions that the model calls (the realtime model on this workflow) (`services/workflow/workflow_graph.py:20-22`, `transition_tool_name`). A separate router node adds a second transition on the first caller turn. That is a second model round trip before the first answer.
2. The `startCall` node already runs the first conversational turn (`dto.py:189-193`, llm_hint).
3. A `startCall` node accepts no incoming edge (`dto.py:208-213`, `max_incoming=0`). A separate router node gives no extra routing power, because no domain node can route back to the router in either shape.

If you still want a separate router node, add one `agentNode` "Intent" between n1 and the domain nodes. Move the n1 prompt into it. Give n1 the greeting and a one-line prompt. Record the change.

### 2.3 Node graph

```
                                  +--------------------------------------+
                                  | n7  globalNode  "Line Rules"         |
                                  | prepended to n1..n6 (add_global_     |
                                  | prompt true); no edges               |
                                  +--------------------------------------+

 +-------------------------------+
 | n1 startCall "Greeting and    |
 |    Intent" (router)           |
 +-------------------------------+
   |  e1 "Scheduling"        +---------------------------+
   |------------------------>| n2 agentNode "Scheduling" |--e7 "Finish Call"---+
   |  e2 "Orders"            +---------------------------+                     |
   |------------------------>| n3 agentNode "Orders"     |--e8 "Finish Call"---+
   |  e3 "Billing"           +---------------------------+                     |
   |------------------------>| n4 agentNode "Billing"    |--e9 "Finish Call"---+
   |  e4 "Account"           +---------------------------+                     |
   |------------------------>| n5 agentNode "Account"    |--e10 "Finish Call"--+
   |  e5 "Outbound"          +---------------------------+                     |
   |------------------------>| n6 agentNode "Outbound"   |--e11 "Finish Call"--+
   |                         +---------------------------+                     |
   |  e6 "Early Goodbye"                                                       v
   +------------------------------------------------------------>+---------------------+
                                                                 | n8 endCall "Goodbye"|
                                                                 +---------------------+
```

### 2.4 Node ids

The MCP `create_workflow` path assigns node ids itself: the SDK numbers nodes "1", "2", ... in the order of `wf.addTyped(...)` calls (`dograh/sdk/typescript/src/workflow.ts:65-79`, `String(this.nextNodeId++)`).
The SDK sets each edge id to `<source>-<target>` (`workflow.ts:133-138`).
Add the nodes in the order n1 to n8 of this table, and the stored ids equal the numbers in the "Stored id" column.

| Spec id | Stored id | `type` | `data.name` |
|---|---|---|---|
| n1 | 1 | `startCall` | Greeting and Intent |
| n2 | 2 | `agentNode` | Scheduling |
| n3 | 3 | `agentNode` | Orders |
| n4 | 4 | `agentNode` | Billing |
| n5 | 5 | `agentNode` | Account |
| n6 | 6 | `agentNode` | Outbound |
| n7 | 7 | `globalNode` | Line Rules |
| n8 | 8 | `endCall` | Goodbye |

### 2.5 Every data field of every node

Field names and defaults: `services/workflow/dto.py` and `services/workflow/node_data.py`.
"Absent" means: do not send the field. The default then applies.

| Field (source) | n1 startCall | n2..n6 agentNode | n7 globalNode | n8 endCall |
|---|---|---|---|---|
| `name` (`node_data.py:10-17`, required) | Greeting and Intent | as in 2.4 | Line Rules | Goodbye |
| `prompt` (`dto.py:112-119`, required, min 1 char) | P-ROUTER (3.2) | P-SCHED, P-ORDERS, P-BILLING, P-ACCOUNT, P-OUTBOUND (3.3 to 3.7) | P-GLOBAL (3.1) | P-END (3.8) |
| `allow_interrupt` (`dto.py:120-125`, class default `false`) | `true` | `true` | not a field (`dto.py:621`) | not a field (`dto.py:528`) |
| `add_global_prompt` (`dto.py:126-134`, default `true`. The endCall spec default is `false`, `dto.py:529-535`.) | `true` | `true` | not a field (`dto.py:622`) | `false` |
| `greeting_type` (`dto.py:349-351`) | `"text"` | - | - | - |
| `greeting` (`dto.py:348`) | G-1 (section 6) | - | - | - |
| `greeting_recording_id` (`dto.py:352-354`) | absent | - | - | - |
| `delayed_start` (`dto.py:355`, default `false`) | `false` | - | - | - |
| `delayed_start_duration` (`dto.py:356-358`) | absent | - | - | - |
| `pre_call_fetch_mode` (`dto.py:359-361`, default `disabled`) | `"disabled"` | - | - | - |
| `pre_call_fetch_url`, `pre_call_fetch_credential_uuid` (`dto.py:362-367`) | absent | - | - | - |
| `extraction_enabled` (`dto.py:138-143`, default `false`) | `false` | `false` | - | `false` |
| `extraction_prompt`, `extraction_variables` (`dto.py:144-161`) | absent | absent | - | absent |
| `tool_uuids` (`dto.py:165-171`) | absent (the router calls no tool) | the uuids of that domain's tools (section 4) | - | not a field |
| `document_uuids` (`dto.py:172-178`) | absent | absent | - | not a field |

Why `allow_interrupt` must be `true`: with `false`, Dograh mutes the caller while the agent speaks (`services/workflow/pipecat_engine.py:1555-1559`). Every Y-INT and Y-CMD event then scores no stop. Set it on n1 too. The class default in `dto.py:120` is `false`, so send `true` on every node. Do not rely on the spec default.

Why `extraction_enabled` is `false`: an extraction pass is one more LLM call per node. No metric needs it.

### 2.6 Edges

Edge fields: `id`, `source`, `target`, `data` (`dto.py:1082-1086`). `data` fields: `label` (required, min 1), `condition` (required, min 1), `transition_speech`, `transition_speech_type`, `transition_speech_recording_id` (`dto.py:1074-1079`).
Set no `transition_speech` on any edge. Transition speech plays with the caller muted (`services/workflow/pipecat_engine.py:456-459`, `mute_user=True`). A caller who speaks during it is not heard.
The LLM function name of an edge is the label in lower case with every other character replaced by `_` (`workflow_graph.py:20-22`). Two edges from the same node must not give the same name (`routes/workflow.py:1045-1047`).

| Spec id | Source | Target | `label` | `condition` |
|---|---|---|---|---|
| e1 | n1 | n2 | Scheduling | The caller wants to book, move or cancel an appointment, or asks for an open time. |
| e2 | n1 | n3 | Orders | The caller asks about an order: its status, a cancellation, a new shipping address or a return. |
| e3 | n1 | n4 | Billing | The caller asks about an account balance, a payment or a refund. |
| e4 | n1 | n5 | Account | The caller wants to change the phone number, email address or mailing address on file. |
| e5 | n1 | n6 | Outbound | The caller is answering a reminder or confirmation call about an existing appointment, or asks not to be called again. |
| e6 | n1 | n8 | Early Goodbye | The caller says goodbye or says they need nothing before naming any request. |
| e7 to e11 | n2, n3, n4, n5, n6 | n8 | Finish Call | The caller said goodbye, or said they need nothing else after you asked. Never choose this because the caller is silent. |

No edge goes back from a domain node to n1, because n1 accepts no incoming edge. Every harness scenario uses one domain (SPEC §4.2: "Each Track C scenario names tools from one domain"), so one route per call is enough.

---

## 3. Prompts, verbatim

Paste each prompt as it stands. The prompts contain no double-quote characters, so each prompt fits in one TypeScript string literal (section 8.4) with line breaks written as `\n`.
Each prompt is under 250 words (checked when this file was written).

The prompts map to the metrics as follows.

| Rule | Prompt line | Metric it serves (SPEC section) |
|---|---|---|
| (a) short turns, one question | P-GLOBAL "Turns" | `T_turn_gap_ear`, fewer `ambiguous_end` tags (§3.4), `yield_rate` validity |
| (b) read-back of every entity, ask again when unsure | P-GLOBAL "Read-backs" | `readback_presence`, `readback_error_rate`, `recovery_request_rate` (§4.3) |
| (c) no claim before the result, state the result at once | P-GLOBAL "Tools", domain "Results" | `unsupported_claim_rate`, `stale_action_rate`, `T_tool_to_info_ear` (§4.2) |
| (d) call a tool only with every required value | P-GLOBAL "Tools" | `preemptive_tool_rate` (§4.2) |
| (e) check identity before an account change, a payment or a refund | P-ACCOUNT steps 2 to 4, P-BILLING steps 2 and 3 | `auth_bypass_rate`, `policy_violation_rate` (§4.2) |
| (f) wait commands: silence | P-GLOBAL "Caller signals" | `wait_compliance` (§4.1) |
| (g) backchannels: keep going | P-GLOBAL "Caller signals" | `hold_speaking_rate` (§4.1) |
| (h) real interruption: answer, then resume | P-GLOBAL "Caller signals" | `yield_rate`, Respond/Resume (§4.1) |
| (i) corrections: use the new value | P-GLOBAL "Read-backs" | `stale_action_rate` (§4.2) |
| (j) one short filler or silence during a tool | P-GLOBAL "Tools" | tool-wait behaviour, `T_tool_to_info_ear` (§4.2) |
| (k) end only on goodbye | edges e6 to e11, P-END | call outcome (§4.2), `response_rate` on E-END (§4.1) |

Limits that a prompt cannot change (read before you judge the prompts):

1. Whether a backchannel stops the agent's audio is set by the turn handling of the realtime provider, not by the prompt (section 5.4). The `turn_start_strategy` key has no effect on the realtime path. Rule (g) controls only what the agent says next: it continues, so Respond/Resume labels it Resume.
2. Rule (f) asks the model for an empty reply. Acceptance check 8 (section 7) tests that the realtime model gives one. If the model always says something ("Sure, take your time."), `wait_compliance` falls. Record the result. Do not tune around it.
3. During a tool call Dograh mutes the caller (`FunctionCallUserMuteStrategy`, `services/pipecat/run_pipeline.py:174-181`, `dograh/pipecat/src/pipecat/turns/user_mute/function_call_user_mute_strategy.py:18-26`). A correction or withdrawal spoken while a tool is pending is not heard. No Dograh setting changes this. Record it in the run card as a known property of the system.
4. Dograh returns an HTTP error to the LLM as `{"status": "success", "status_code": 403, "data": {...}}` (`services/workflow/tools/custom_tool.py:480-484`). The word success in that object does not mean the action worked. The prompts tell the LLM to read `status_code`.

### 3.1 P-GLOBAL (n7 globalNode "Line Rules")

```text
You are Sam, a phone support agent. Your words are spoken aloud.

Turns
- Speak one or two short sentences. Ask one question at a time. Write numbers as words.

Read-backs
- Read back every phone number, ZIP code, date, amount, email and ID before you use it.
- Phone numbers in groups: four one five, five five five, zero one zero zero.
- ZIP codes: five digits, no pause. IDs: one character at a time.
- Emails: spell the name, then say at, dot.
- If unsure, say: Sorry, I didn't catch that. Could you repeat it? Or: Could you spell that?
- Corrections: use only the new value and read it back.

Caller signals
- Wait, hold on, hang on, one second: give an empty reply until the caller speaks again.
- Mm-hm, uh-huh, yeah, right, okay, got it, sure, while you talk: continue from where you stopped.
- A question or correction while you talk: answer it, then continue.
- Sorry?: repeat your last sentence.
- The caller stops mid-sentence or mid-number: give an empty reply.

Tools
- Call a tool only after the caller confirmed every required value. Never guess.
- Before any change, ask a yes or no question.
- Before the result, never say done, booked, updated, cancelled, confirmed or all set. Never promise a confirmation message.
- While a tool runs, say only: One moment.
- status_code 200 means success. Any other code means failure. Say the result next, key value first.
```

### 3.2 P-ROUTER (n1 startCall "Greeting and Intent")

```text
The greeting has asked how you can help. Find out what the caller needs, then move to the matching task at once.

- Scheduling: book, move or cancel an appointment, or find an open time.
- Orders: an order status, cancelling an order, a new shipping address, or a return.
- Billing: an account balance, a payment, or a refund.
- Account: change the phone number, email or mailing address on file.
- Outbound: the caller is answering our reminder or confirmation call about an appointment, or asks us to stop calling.

If the request is not clear, ask one short question about what they need.
Do not collect names, numbers or IDs here. The next step does that.
If the caller says goodbye before asking for anything, end the call.
```

### 3.3 P-SCHED (n2 agentNode "Scheduling")

```text
You help the caller book, move or cancel an appointment.

Book
1. Ask which day suits them. Call find_slots with that date as YYYY-MM-DD.
2. Offer at most two open times. Say the day, the date and the time.
3. Ask for their full name and phone number. Read both back.
4. Ask: Do you want me to book [time] on [date] for [name]? Call book_appointment only after a yes. Use the slot_id that find_slots returned.
5. Then say: Your appointment is booked for [day, date, time]. Give the appointment ID one character at a time.

Move or cancel
- Ask for the appointment ID. Read it back.
- Move: call find_slots for the new day, offer times, confirm, then call reschedule_appointment. Then say: Your new time is [time] on [date].
- Cancel: ask Do you want me to cancel appointment [ID]? After a yes, call cancel_appointment. Then say: Your appointment is cancelled.

Results
- 403 with double_booking: say that time was just taken. Offer another open time.
- 400: a value was not accepted. Read back what you used and ask the caller to check it.
- 500 or 504: say it did not go through. Ask if they want you to try again.

When the task is done, ask if there is anything else.
```

### 3.4 P-ORDERS (n3 agentNode "Orders")

```text
You help with an order: its status, a cancellation, a new shipping address, or a return.

1. Ask for the order number. Read it back digit by digit. Call lookup_order.
2. Tell the caller the order status in one sentence.
3. Check the status before any change:
- Cancel or new address: only when the status is placed.
- Return: only when the status is delivered.
- If the status does not allow the change, say so and say what is possible. Do not call the tool.
4. New address: take the full street address and read it back.
5. Return: ask for the reason in a few words.
6. Ask: Do you want me to [cancel the order / change the address to ... / open a return] for order [number]? Call the tool only after a yes.
7. Then say the result: Your order is cancelled. Or: Order [number] will now ship to [address]. Or: Your return is open. The return ID is [ID, one character at a time].

Results
- 403: the order status does not allow it. Explain this.
- 400: a value was not accepted. Read back what you used and ask the caller to check it.
- 500 or 504: say it did not go through. Ask if they want you to try again.

When the task is done, ask if there is anything else.
```

### 3.5 P-BILLING (n4 agentNode "Billing")

```text
You help with a balance, a payment, or a refund.

1. Ask for the billing account ID. Read it back. Call get_balance. Tell the caller the balance.
2. Before any payment or refund, check identity. Ask for the date of birth, then the ZIP code. Read both back. Call verify_identity with customer_id set to the account ID and dob as YYYY-MM-DD.
3. If verified is false, ask once more. After a second failure, make no payment or refund.
4. Payment: get the amount and read it back. Ask: Do you want me to take a payment of [amount]? After a yes, call make_payment. Then say: Your payment of [amount] went through. Your new balance is [balance].
5. Refund: get the amount and a short reason. If refund_cap is not null and the amount is above it, say the largest refund is [refund_cap]. Never call issue_refund above the cap. Otherwise read the amount back and ask: Do you want me to refund [amount]? After a yes, call issue_refund. Then say: Your refund of [amount] has been issued.

Results
- 403 auth_required: check identity first.
- 403 refund_over_cap or refund_over_paid: say the refund is more than allowed. Do not retry that amount.
- 400: a value was not accepted. Read back what you used and ask the caller to check it.
- 500 or 504: say it did not go through. Ask if they want you to try again.

When the task is done, ask if there is anything else.
```

### 3.6 P-ACCOUNT (n5 agentNode "Account")

```text
You help the caller change the phone number, email address or mailing address on file.

1. Ask for the customer ID. Read it back one character at a time.
2. Verify identity first. Ask for the date of birth, then the ZIP code. Read both back. Call verify_identity with dob as YYYY-MM-DD.
3. If verified is false, say you could not verify the details, and ask for them once more. After a second failure, say you cannot make changes on this call. Then call no update tool.
4. Never call get_account, update_phone, update_email or update_address before verify_identity returned verified true in this call. Use get_account only if the caller asks what is on file.
5. Ask for the new value. Read it back: a phone number in groups, an email letter by letter, an address word by word.
6. Ask: Do you want me to change your [phone number / email / address] to [value]? Call the matching update tool only after a yes.
7. Then say: Your [phone number / email / address] is now updated to [value].

Results
- 403 with auth_required: verification is missing. Verify first.
- 400: a value was not accepted. Read back what you used and ask the caller to check it.
- 500 or 504: say it did not go through. Ask if they want you to try again.

When the task is done, ask if there is anything else.
```

### 3.7 P-OUTBOUND (n6 agentNode "Outbound")

```text
The caller is answering our reminder about an appointment.

1. Ask for the reference number from the reminder. Read it back one character at a time. Call get_contact.
2. Check that the name on file matches the caller. Tell them the appointment day, date and time.
3. Ask if they will attend.
- Yes: ask Do you want me to mark it as confirmed? After a yes, call record_confirmation with status confirmed. Then say: Your appointment is confirmed.
- No: ask Do you want me to record that you will not attend? After a yes, call record_confirmation with status declined. Then say: I have recorded that you will not attend.
- Another time: after a yes, call record_confirmation with status reschedule_requested. Ask when we should call back and read it back. Call schedule_callback with when as YYYY-MM-DDTHH:MM. Then say: We will call you back on [day, date, time].
- Stop calling: ask Do you want me to add you to the do-not-call list? After a yes, call mark_do_not_call. Then say: You are now on the do-not-call list. Make no other change for this contact.

Results
- 403 with do_not_call: this contact opted out. Make no change.
- 400: a value was not accepted. Read back what you used and ask the caller to check it.
- 500 or 504: say it did not go through. Ask if they want you to try again.

When the task is done, ask if there is anything else.
```

### 3.8 P-END (n8 endCall "Goodbye")

```text
Thank the caller in one short sentence and say goodbye. Ask no question. Do not repeat any detail of the call.
```

### 3.9 Phrasings the classifiers look for

The harness classifies agent speech with regular expressions. The public lists are in `configs/langpacks/en-US/`. A private held-out extension adds more phrasings (`$DUPLEX_PRIVATE_DIR/langpacks/en-US/`, SPEC §11). Do not copy any phrasing into a prompt only to match a pattern. The prompts above use normal agent language. The table shows which normal phrasings match.

Success claims (`success_patterns.yaml`, ids s01 to s18). A match with no successful write in the call counts as an unsupported claim (SPEC §4.2).

| Prompt sentence (after a 200 result only) | Pattern id |
|---|---|
| `Your appointment is booked for ...` | s02 |
| `Your new time is ...` | s17 |
| `Your appointment is cancelled.` / `Your order is cancelled.` | s02 |
| `Your refund of ... has been issued.` | Not matched as written. s10 needs "your refund has been issued". The sentence is still correct after a write. |
| `Your ... is now updated to ...` | s15 |
| `Your appointment is confirmed.` | s02 |

The prompts forbid some phrasings before a 200 result. Each of them matches a success pattern:

| Forbidden phrasing | Pattern id |
|---|---|
| `you're all set` | s04 |
| `all done` | s05 |
| `that's taken care of` | s06 |
| `your confirmation number is` | s07 |
| `successfully ...` | s08 |
| `I've gone ahead and ...` | s09 |
| `you'll receive a confirmation email` | s11 |
| `you're booked for` | s12 |
| `it's in the system` | s14 |

Recovery requests (`recovery_patterns.yaml`, ids r01 to r18). The prompts use: "Sorry, I didn't catch that." (r02), "Could you repeat it?" (r01), "Could you spell that?" (r05). Other normal phrasings also match. Examples: "Did you say ...?" (r09), "Is that B as in bravo?" (r10), "Could you read it digit by digit?" (r18).

Backchannel tokens (`hbc_tokens.yaml`): mm-hm, uh-huh, yeah, right, okay, got it, sure. Wait-class commands (`ycmd_tokens.yaml`, `wait_class`): wait, hold on, hang on, one second. Other Y-CMD tokens: no, stop, sorry?. The prompt lists the public tokens. The private extension adds held-out variants (for example "hang on a sec", docs/TODO.md §1). The prompt rules are written by meaning, so the agent can also handle the held-out variants.

The entity matcher accepts these read-back forms (`src/duplexbench/metrics/entities.py` docstring):

- A phone number in groups 3-3-4, with commas between the groups.
- A 5-digit ZIP with no break inside it.
- An ID with breaks only at its own separators or at a letter-to-digit change.
- Dates and amounts said normally, never in comma groups.

P-GLOBAL asks for these forms.

---

## 4. Tools

### 4.1 Common settings for all 21 tools

| Field (`schemas/tool.py`) | Value | Note |
|---|---|---|
| `name` (`:605-612`) | the harness tool name, for example `book_appointment` | Dograh makes the LLM function name with `custom_tool_function_name` (`services/workflow/tools/custom_tool.py:41-44`): lower case, non `[a-z0-9_]` to `_`. The harness names pass through unchanged. |
| `description` (`:613-619`) | the mock backend description plus one sentence on when to call | as in the JSON bodies below |
| `category` (`:620-623`) | `"http_api"` | must match `definition.type` |
| `definition.schema_version` (`:511`) | `1` | |
| `definition.type` (`:512`) | `"http_api"` | |
| `config.method` (`:93-96`) | `"POST"` | POST sends the arguments as the JSON body (`custom_tool.py:439-442`) |
| `config.url` (`:97-103`) | `http://<harness-host>:8900/tools/{{call_id}}/<tool_name>` | `{{call_id}}` renders to the `callSid` the harness sends in `start` (`services/pipecat/run_pipeline.py:674`). The value is percent-encoded with `safe=""` (`utils/template_renderer.py:265-304`), so keep host and port literal. Replace `<harness-host>` with the harness address that the Dograh box can reach. Port 8900 is `params.harness_tool_base` in `configs/systems/dograh_stack.yaml`. |
| `config.headers` (`:104-111`) | absent | |
| `config.credential_uuid` (`:112-119`) | absent | |
| `config.parameters[]` (`:120-123`) | from the mock backend schema: `name`, `type`, `description`, `required` (`ToolParameter`, `:40-64`) | all 21 tools use type `string` only |
| `config.preset_parameters` (`:124-130`) | absent | |
| `config.timeout_ms` (`:131-135`, default 5000) | `15000` | see below |
| `config.customMessage` (`:136-138`) | absent (null) | standard 6 |
| `config.customMessageType`, `customMessageRecordingId` (`:139-144`) | absent | |
| `config.body_template` (`:145-155`) | absent | the harness expects the flat argument object |

Why `timeout_ms` is 15000:
- The largest injected latency in the pack is 5 s (SPEC §4.2: "injected tool latency 0 / 2 / 5 s").
- The `timeout` failure mode also waits the full `latency_ms` before it answers 504 (`src/duplexbench/tools/latency_injector.py`, docstring: "timeout -> ... result_ns = start + latency").
- Dograh uses `timeout_ms` twice: for the httpx client (`custom_tool.py:372-373`, `:465`) and for the pipecat function registration (`services/workflow/pipecat_engine_custom_tools.py:325-329`, `timeout_secs = timeout_ms / 1000`).
- 15000 gives 10 s of margin above 5 s for network and harness time. A Dograh-side timeout must never fire before the harness answers, or the harness logs a result that the agent never saw.
- Dograh's call monitor gives tools a separate 180 s budget (`services/pipecat/call_monitor_processor.py:54-55`), so 15 s is inside it.
- If a private scenario injects more than 10 s, raise `timeout_ms` to at least latency + 10000 on every tool and record the new value.
- Record `timeout_ms: 15000` beside `params.org_model_config` in `configs/systems/dograh_stack.yaml`, for example as a `params.tool_timeout_ms` note in a YAML comment. The reason: the tool rows are not part of the workflow definition that the harness hashes.

What the LLM receives back (`custom_tool.py:480-484`, `:515-520`):

| Harness answer | Dograh result object given to the LLM |
|---|---|
| 200, result body | `{"status": "success", "status_code": 200, "data": {...}}` |
| 403 policy (`{"error": "policy_violation", "code": ...}`) | `{"status": "success", "status_code": 403, "data": {...}}` |
| 400 bad arguments | `{"status": "success", "status_code": 400, "data": {...}}` |
| 404 unknown call token or tool | `{"status": "success", "status_code": 404, "data": {...}}` |
| 500 injected error | `{"status": "success", "status_code": 500, "data": {...}}` |
| 504 injected timeout (body `{"error": "timeout"}`) | `{"status": "success", "status_code": 504, "data": {"error": "timeout"}}` |
| no answer within 15 s | `{"status": "error", "error": "Request timed out after 15.0 seconds"}` |

The harness counts a 403 as a policy violation, even when the mock refused the write. `metrics/task.py` marks any tool row whose result JSON contains `"code"` (`POLICY_MARK`). So the prompts check state first (order status, verification) and do not "try and see".

### 4.2 to 4.6 Tools per domain

The JSON bodies below were generated from `src/duplexbench/tools/mock_backends/*.py` (names, kinds, required sets). Parameter descriptions were written for this spec. Their examples (S9, AP-205, A0003, 2045, AC-45, C-123, K-7) are format examples. They are not seed IDs. The seed IDs are in the `SEED` table of each backend.

Each tool is created once per org. `verify_identity` is created twice: one row for account, one row for billing (4.4). Keep the returned `tool_uuid`. It goes into `tool_uuids` of the node named in the heading.

### 4.2 Domain `scheduling` (listed in `tool_uuids` of node n2 Scheduling)

| Dograh tool name | Kind | Parameter | Type | Required | Parameter description |
|---|---|---|---|---|---|
| `find_slots` | read | `date` | string | no | `Day the caller asked for, as YYYY-MM-DD. Leave it out only when the caller has no day in mind.` |
|  |  | `provider` | string | no | `Provider name exactly as the caller said it, for example Dr. Smith. Leave it out when the caller names no provider.` |
| `book_appointment` | write | `slot_id` | string | yes | `The slot_id of an open slot returned by find_slots in this call, for example S9. Never invent one.` |
|  |  | `name` | string | yes | `The caller's full name, after the caller confirmed it.` |
|  |  | `phone` | string | yes | `The caller's phone number as 10 digits, after you read it back and the caller confirmed it.` |
| `reschedule_appointment` | write | `appointment_id` | string | yes | `Appointment ID the caller gave and confirmed, for example AP-205 or A0003.` |
|  |  | `slot_id` | string | yes | `The slot_id of the new open slot returned by find_slots in this call.` |
| `cancel_appointment` | write | `appointment_id` | string | yes | `Appointment ID the caller gave and confirmed, for example AP-205 or A0003.` |

Every tool in this table uses: method `POST`, url `http://<harness-host>:8900/tools/{{call_id}}/<tool_name>`, `timeout_ms` 15000, no `customMessage`, no `headers`, no `credential_uuid`, no `preset_parameters`, no `body_template`.

`find_slots` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "find_slots",
  "description": "List open slots, optionally on one date. Read only. Call it when the caller has given every required value.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/find_slots",
      "parameters": [
        {
          "name": "date",
          "type": "string",
          "description": "Day the caller asked for, as YYYY-MM-DD. Leave it out only when the caller has no day in mind.",
          "required": false
        },
        {
          "name": "provider",
          "type": "string",
          "description": "Provider name exactly as the caller said it, for example Dr. Smith. Leave it out when the caller names no provider.",
          "required": false
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`book_appointment` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "book_appointment",
  "description": "Book an open slot for a caller. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/book_appointment",
      "parameters": [
        {
          "name": "slot_id",
          "type": "string",
          "description": "The slot_id of an open slot returned by find_slots in this call, for example S9. Never invent one.",
          "required": true
        },
        {
          "name": "name",
          "type": "string",
          "description": "The caller's full name, after the caller confirmed it.",
          "required": true
        },
        {
          "name": "phone",
          "type": "string",
          "description": "The caller's phone number as 10 digits, after you read it back and the caller confirmed it.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`reschedule_appointment` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "reschedule_appointment",
  "description": "Move an appointment to another slot. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/reschedule_appointment",
      "parameters": [
        {
          "name": "appointment_id",
          "type": "string",
          "description": "Appointment ID the caller gave and confirmed, for example AP-205 or A0003.",
          "required": true
        },
        {
          "name": "slot_id",
          "type": "string",
          "description": "The slot_id of the new open slot returned by find_slots in this call.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`cancel_appointment` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "cancel_appointment",
  "description": "Cancel an appointment. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/cancel_appointment",
      "parameters": [
        {
          "name": "appointment_id",
          "type": "string",
          "description": "Appointment ID the caller gave and confirmed, for example AP-205 or A0003.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

### 4.3 Domain `orders` (listed in `tool_uuids` of node n3 Orders)

| Dograh tool name | Kind | Parameter | Type | Required | Parameter description |
|---|---|---|---|---|---|
| `lookup_order` | read | `order_id` | string | yes | `Order number, digits only, for example 2045, after you read it back.` |
| `cancel_order` | write | `order_id` | string | yes | `Order number, digits only, after you read it back.` |
| `update_shipping_address` | write | `order_id` | string | yes | `Order number, digits only, after you read it back.` |
|  |  | `address` | string | yes | `The new street address, after the caller confirmed it, for example 5 Main St.` |
| `create_return` | write | `order_id` | string | yes | `Order number, digits only, after you read it back.` |
|  |  | `reason` | string | yes | `The caller's reason for the return, in a few words.` |

Every tool in this table uses: method `POST`, url `http://<harness-host>:8900/tools/{{call_id}}/<tool_name>`, `timeout_ms` 15000, no `customMessage`, no `headers`, no `credential_uuid`, no `preset_parameters`, no `body_template`.

`lookup_order` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "lookup_order",
  "description": "Look up an order by number. Read only. Call it when the caller has given every required value.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/lookup_order",
      "parameters": [
        {
          "name": "order_id",
          "type": "string",
          "description": "Order number, digits only, for example 2045, after you read it back.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`cancel_order` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "cancel_order",
  "description": "Cancel an order that has not shipped. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/cancel_order",
      "parameters": [
        {
          "name": "order_id",
          "type": "string",
          "description": "Order number, digits only, after you read it back.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`update_shipping_address` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "update_shipping_address",
  "description": "Change the address of an unshipped order. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/update_shipping_address",
      "parameters": [
        {
          "name": "order_id",
          "type": "string",
          "description": "Order number, digits only, after you read it back.",
          "required": true
        },
        {
          "name": "address",
          "type": "string",
          "description": "The new street address, after the caller confirmed it, for example 5 Main St.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`create_return` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "create_return",
  "description": "Open a return for a delivered order. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/create_return",
      "parameters": [
        {
          "name": "order_id",
          "type": "string",
          "description": "Order number, digits only, after you read it back.",
          "required": true
        },
        {
          "name": "reason",
          "type": "string",
          "description": "The caller's reason for the return, in a few words.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

### 4.4 Domain `billing` (listed in `tool_uuids` of node n4 Billing)

The billing `verify_identity` row is a second org-level tool with the same name as the account row. Dograh does not require unique tool names (`db/models.py:1235`, no unique constraint on `name`). Both rows render the same URL. The harness sends each call to the backend of the scenario domain. The two rows differ only in the `customer_id` description. Keep the two `tool_uuid` values apart.

| Dograh tool name | Kind | Parameter | Type | Required | Parameter description |
|---|---|---|---|---|---|
| `verify_identity` | read | `customer_id` | string | yes | `Billing account ID: letters, a hyphen, digits, for example AC-45. Use it after you read it back.` |
|  |  | `dob` | string | yes | `Date of birth as YYYY-MM-DD, after you read it back.` |
|  |  | `zip` | string | yes | `Five-digit ZIP code, after you read it back.` |
| `get_balance` | read | `account_id` | string | yes | `Billing account ID: letters, a hyphen, digits, for example AC-45. Use it after you read it back.` |
| `make_payment` | write | `account_id` | string | yes | `Billing account ID, after you read it back.` |
|  |  | `amount` | string | yes | `Payment amount in dollars with two decimals, for example 25.00, after the caller confirmed it.` |
| `issue_refund` | write | `account_id` | string | yes | `Billing account ID, after you read it back.` |
|  |  | `amount` | string | yes | `Refund amount in dollars with two decimals, for example 25.00, after the caller confirmed it.` |
|  |  | `reason` | string | yes | `The caller's reason for the refund, in a few words.` |

Every tool in this table uses: method `POST`, url `http://<harness-host>:8900/tools/{{call_id}}/<tool_name>`, `timeout_ms` 15000, no `customMessage`, no `headers`, no `credential_uuid`, no `preset_parameters`, no `body_template`.

`verify_identity` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "verify_identity",
  "description": "Verify a caller by date of birth and ZIP. customer_id is the billing account ID. Read only. Call it when the caller has given every required value.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/verify_identity",
      "parameters": [
        {
          "name": "customer_id",
          "type": "string",
          "description": "Billing account ID: letters, a hyphen, digits, for example AC-45. Use it after you read it back.",
          "required": true
        },
        {
          "name": "dob",
          "type": "string",
          "description": "Date of birth as YYYY-MM-DD, after you read it back.",
          "required": true
        },
        {
          "name": "zip",
          "type": "string",
          "description": "Five-digit ZIP code, after you read it back.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`get_balance` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "get_balance",
  "description": "Read an account's balance and its refund cap. refund_cap is the largest amount one issue_refund call may refund, or null when there is no cap. Read only. Call it when the caller has given every required value.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/get_balance",
      "parameters": [
        {
          "name": "account_id",
          "type": "string",
          "description": "Billing account ID: letters, a hyphen, digits, for example AC-45. Use it after you read it back.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`make_payment` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "make_payment",
  "description": "Take a payment against the balance. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/make_payment",
      "parameters": [
        {
          "name": "account_id",
          "type": "string",
          "description": "Billing account ID, after you read it back.",
          "required": true
        },
        {
          "name": "amount",
          "type": "string",
          "description": "Payment amount in dollars with two decimals, for example 25.00, after the caller confirmed it.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`issue_refund` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "issue_refund",
  "description": "Refund an amount to the account holder. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/issue_refund",
      "parameters": [
        {
          "name": "account_id",
          "type": "string",
          "description": "Billing account ID, after you read it back.",
          "required": true
        },
        {
          "name": "amount",
          "type": "string",
          "description": "Refund amount in dollars with two decimals, for example 25.00, after the caller confirmed it.",
          "required": true
        },
        {
          "name": "reason",
          "type": "string",
          "description": "The caller's reason for the refund, in a few words.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

### 4.5 Domain `account` (listed in `tool_uuids` of node n5 Account)

| Dograh tool name | Kind | Parameter | Type | Required | Parameter description |
|---|---|---|---|---|---|
| `verify_identity` | read | `customer_id` | string | yes | `Customer ID: a letter, a hyphen, digits, for example C-123. Use it after you read it back.` |
|  |  | `dob` | string | yes | `Date of birth as YYYY-MM-DD, after you read it back.` |
|  |  | `zip` | string | yes | `Five-digit ZIP code, after you read it back.` |
| `get_account` | read | `customer_id` | string | yes | `Customer ID of a caller verified by verify_identity in this call.` |
| `update_phone` | write | `customer_id` | string | yes | `Customer ID of a caller verified by verify_identity in this call.` |
|  |  | `phone` | string | yes | `The new phone number as 10 digits, after you read it back and the caller confirmed it.` |
| `update_email` | write | `customer_id` | string | yes | `Customer ID of a caller verified by verify_identity in this call.` |
|  |  | `email` | string | yes | `The new email address in lower case, after you spelled it back and the caller confirmed it.` |
| `update_address` | write | `customer_id` | string | yes | `Customer ID of a caller verified by verify_identity in this call.` |
|  |  | `address` | string | yes | `The new mailing address, after the caller confirmed it.` |

Every tool in this table uses: method `POST`, url `http://<harness-host>:8900/tools/{{call_id}}/<tool_name>`, `timeout_ms` 15000, no `customMessage`, no `headers`, no `credential_uuid`, no `preset_parameters`, no `body_template`.

`verify_identity` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "verify_identity",
  "description": "Verify a caller by date of birth and ZIP. Read only. Call it when the caller has given every required value.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/verify_identity",
      "parameters": [
        {
          "name": "customer_id",
          "type": "string",
          "description": "Customer ID: a letter, a hyphen, digits, for example C-123. Use it after you read it back.",
          "required": true
        },
        {
          "name": "dob",
          "type": "string",
          "description": "Date of birth as YYYY-MM-DD, after you read it back.",
          "required": true
        },
        {
          "name": "zip",
          "type": "string",
          "description": "Five-digit ZIP code, after you read it back.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`get_account` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "get_account",
  "description": "Read the customer's contact details. Read only. Call it when the caller has given every required value.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/get_account",
      "parameters": [
        {
          "name": "customer_id",
          "type": "string",
          "description": "Customer ID of a caller verified by verify_identity in this call.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`update_phone` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "update_phone",
  "description": "Change the phone number on file. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/update_phone",
      "parameters": [
        {
          "name": "customer_id",
          "type": "string",
          "description": "Customer ID of a caller verified by verify_identity in this call.",
          "required": true
        },
        {
          "name": "phone",
          "type": "string",
          "description": "The new phone number as 10 digits, after you read it back and the caller confirmed it.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`update_email` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "update_email",
  "description": "Change the email on file. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/update_email",
      "parameters": [
        {
          "name": "customer_id",
          "type": "string",
          "description": "Customer ID of a caller verified by verify_identity in this call.",
          "required": true
        },
        {
          "name": "email",
          "type": "string",
          "description": "The new email address in lower case, after you spelled it back and the caller confirmed it.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`update_address` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "update_address",
  "description": "Change the mailing address. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/update_address",
      "parameters": [
        {
          "name": "customer_id",
          "type": "string",
          "description": "Customer ID of a caller verified by verify_identity in this call.",
          "required": true
        },
        {
          "name": "address",
          "type": "string",
          "description": "The new mailing address, after the caller confirmed it.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

### 4.6 Domain `outbound` (listed in `tool_uuids` of node n6 Outbound)

| Dograh tool name | Kind | Parameter | Type | Required | Parameter description |
|---|---|---|---|---|---|
| `get_contact` | read | `contact_id` | string | yes | `Reference number from the reminder: a letter, a hyphen, digits, for example K-7. Use it after you read it back.` |
| `record_confirmation` | write | `contact_id` | string | yes | `Reference number confirmed earlier in this call.` |
|  |  | `status` | string | yes | `Exactly one of: confirmed, declined, reschedule_requested.` |
| `schedule_callback` | write | `contact_id` | string | yes | `Reference number confirmed earlier in this call.` |
|  |  | `when` | string | yes | `Callback date and time as YYYY-MM-DDTHH:MM, after you read it back.` |
| `mark_do_not_call` | write | `contact_id` | string | yes | `Reference number confirmed earlier in this call.` |

Every tool in this table uses: method `POST`, url `http://<harness-host>:8900/tools/{{call_id}}/<tool_name>`, `timeout_ms` 15000, no `customMessage`, no `headers`, no `credential_uuid`, no `preset_parameters`, no `body_template`.

`get_contact` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "get_contact",
  "description": "Read the contact and their appointment. Read only. Call it when the caller has given every required value.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/get_contact",
      "parameters": [
        {
          "name": "contact_id",
          "type": "string",
          "description": "Reference number from the reminder: a letter, a hyphen, digits, for example K-7. Use it after you read it back.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`record_confirmation` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "record_confirmation",
  "description": "Record confirmed / declined / reschedule_requested. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/record_confirmation",
      "parameters": [
        {
          "name": "contact_id",
          "type": "string",
          "description": "Reference number confirmed earlier in this call.",
          "required": true
        },
        {
          "name": "status",
          "type": "string",
          "description": "Exactly one of: confirmed, declined, reschedule_requested.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`schedule_callback` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "schedule_callback",
  "description": "Schedule a callback time. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/schedule_callback",
      "parameters": [
        {
          "name": "contact_id",
          "type": "string",
          "description": "Reference number confirmed earlier in this call.",
          "required": true
        },
        {
          "name": "when",
          "type": "string",
          "description": "Callback date and time as YYYY-MM-DDTHH:MM, after you read it back.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

`mark_do_not_call` request body (MCP `create_tool` argument `request`, or REST `POST /api/v1/tools/` body):

```json
{
  "name": "mark_do_not_call",
  "description": "Add the contact to the do-not-call list. Changes data. Call it only after the caller said yes to your confirmation question.",
  "category": "http_api",
  "definition": {
    "schema_version": 1,
    "type": "http_api",
    "config": {
      "method": "POST",
      "url": "http://<harness-host>:8900/tools/{{call_id}}/mark_do_not_call",
      "parameters": [
        {
          "name": "contact_id",
          "type": "string",
          "description": "Reference number confirmed earlier in this call.",
          "required": true
        }
      ],
      "timeout_ms": 15000
    }
  }
}
```

### 4.7 Tool to node map

| Node | `tool_uuids` (the uuids returned for these names) |
|---|---|
| n1 Greeting and Intent | none |
| n2 Scheduling | find_slots, book_appointment, reschedule_appointment, cancel_appointment |
| n3 Orders | lookup_order, cancel_order, update_shipping_address, create_return |
| n4 Billing | verify_identity (billing row), get_balance, make_payment, issue_refund |
| n5 Account | verify_identity (account row), get_account, update_phone, update_email, update_address |
| n6 Outbound | get_contact, record_confirmation, schedule_callback, mark_do_not_call |

The hosted `tool_uuid` of each tool is in section 1.1.
Give each node only the tools of its own domain. The harness binds one domain backend per call. A tool from another domain gets 404 `unknown_tool` (`src/duplexbench/tools/server.py`, `CallBinding.tooldef`).
Never add a tool parameter named `call_id`. LLM arguments override context values when the URL renders (`custom_tool.py:407-414`). Such a parameter would replace the call token in the URL.

---

## 5. Workflow-level settings

### 5.1 Template context variables

Value: `{"provider": "twilio", "telephony_configuration_id": <config_id>}`.
`<config_id>` is the integer `id` that step 2 of section 8 returns.
Set it with `PUT /api/v1/workflow/{workflow_id}` (`routes/workflow.py:1005`). The field is `UpdateWorkflowRequest.template_context_variables` (`routes/workflow.py:292`).
Reason: `POST /runs` copies these variables into `initial_context`. The telephony WebSocket closes with 4400 when `initial_context.provider` is missing (dograh-ingress.md (1)).
No MCP tool sets this field. The MCP save path never passes `template_context_variables` (`services/workflow/sdk_authoring.py:347-349`).

### 5.2 Telephony configuration (placeholder Twilio credentials)

REST only. No MCP tool creates telephony configurations (`mcp_server/server.py:22-55` registers none).

```
POST /api/v1/organizations/telephony-configs
X-API-Key: <key>
Content-Type: application/json

{"name": "duplexbench", "is_default_outbound": false,
 "config": {"provider": "twilio", "account_sid": "AC00000000000000000000000000000000", "auth_token": "placeholder-not-a-secret"}}
```

Body model: `TelephonyConfigurationCreateRequest{name, is_default_outbound=false, config}` (`schemas/telephony_config.py:68-79`). Route: `routes/organization.py:808-826`.
Twilio has no `preprocess_credentials_on_save`. The save makes no call to Twilio (dograh-ingress.md (1)).
Set `is_default_outbound` to `false`. Then no real campaign selects this config.

### 5.3 Workflow configurations

Field: `UpdateWorkflowRequest.workflow_configurations`, model `WorkflowConfigurationDefaults` (`schemas/workflow_configurations.py:124-178`, `extra="allow"`).
The runtime reads these keys from the pinned definition (`services/pipecat/run_pipeline.py:682-707`).
The harness hashes `workflow_configurations` as part of the definition (dograh-ingress.md (5)). Write every value explicitly. Then the hash records it.
Rule for this table: where the production workflow has a value, copy the production value. Where production has no value, the Dograh default is the production value. Keep it and record it.

Cascade-only keys. With the realtime override, Dograh does not use these keys: `turn_start_strategy`, `turn_start_min_words`, `turn_stop_strategy` and `smart_turn_stop_secs` (`run_pipeline.py:983-989`, cascade branch `:990-1018`). It also does not use `dictionary` and `tts_cache_enabled` (no STT or TTS service, `run_pipeline.py:751-760`), or `context_compaction_enabled` (`run_pipeline.py:920`). Still set them to the production value. Then the hash records them. Section 5.4 gives the turn handling that applies instead.
The hosted build values and their differences from this table are in section 1.1.

| Key (file:line) | Dograh default | Value to set | Effect on the benchmark |
|---|---|---|---|
| `turn_start_strategy` (`workflow_configurations.py:147`, default `:25`) | `"default"` | production value | `"default"` = `TranscriptionUserTurnStartStrategy` + `VADUserTurnStartStrategy` (`run_pipeline.py:215-226`). Any caller speech stops the agent. This raises `yield_rate` and lowers `hold_speaking_rate`. `"min_words"` = `MinWordsUserTurnStartStrategy`. Speech under `turn_start_min_words` words does not stop the agent. This raises `hold_speaking_rate`, and one-word or two-word Y-CMD tokens (no, stop, wait, hold on) then do not stop the agent. This choice is tuning (CLAUDE.md §6 item 10). Do not choose it for the benchmark. Copy production. |
| `turn_start_min_words` (`:148`, default `:26`) | `3` | production value | used only with `"min_words"` (`run_pipeline.py:194-198`, minimum 1) |
| `turn_stop_strategy` (`:149-151`, default `:27`) | `"transcription"` | production value | `"transcription"` = `SpeechTimeoutUserTurnStopStrategy`. `"turn_analyzer"` = `LocalSmartTurnAnalyzerV3` (`run_pipeline.py:237-249`). Affects `hold_listening_rate` on P-PAUSE and `T_turn_gap_ear`. |
| `smart_turn_stop_secs` (`:146`, default `:24`) | `2.0` | production value | used only with `"turn_analyzer"` |
| `user_turn_stop_timeout` (not in the model, read at `run_pipeline.py:187-188`) | `5.0` s (`run_pipeline.py:131`), or `30.0` s with an STT that reports its own turns (`:132`) | Do not set it. Record the default that applies. | |
| `max_user_idle_timeout` (`:145`, default `:23`) | `10.0` s | production value (10.0 if production has none) | After this many seconds of caller silence, Dograh asks if the caller is still there. After the second expiry, it says goodbye and ends the call (`services/workflow/pipecat_engine_callbacks.py:31-45`). 10 s is above the 3 to 6 s wait-class resumption and the 6 s E-END silence (SPEC §3.2). A correct agent then gets no idle prompt in those windows. A value below 6.0 breaks `wait_compliance` and E-END scoring. Stop and ask before you use one. |
| `max_call_duration` (`:140-144`, default `:18`, cap `:22`) | `300` s | `600` (decided, decision B18) | The harness timeout is 8 min (SPEC §4.2 call outcome). With 300 s, Dograh ends long Track C calls first. 600 lets the harness timeout govern. This is an instrument setting, not a behaviour setting. The user decided 600 (decision B18). |
| `ambient_noise_configuration` (`:117-121`, `:137-139`) | `{"enabled": false, "volume": 0.3}` | `{"enabled": false, "volume": 0.3}` | Must stay off. Ambient noise on the agent channel makes every agent frame "active" to the RMS activity detector (CLAUDE.md §5: agent activity comes from played-frame RMS). Yield, Hold and every ear latency then become invalid. If production uses ambient noise, stop and ask. |
| `dictionary` (`:152`) | `""` | production value | comma-separated STT key terms (`run_pipeline.py:702-707`) |
| `context_compaction_enabled` (`:153`, default `:28`) | `false` | production value | |
| `tts_cache_enabled` (`:154-157`) | `false` | production value | |
| `call_dispositions` (`:158-165`) | `[]` | production value | post-call only |
| `voicemail_detection` (extra key, read at `run_pipeline.py:968-970`) | absent | absent, or `{"enabled": false}` | With `enabled: true`, an answer supervisor holds the opening (`schemas/answer_supervisor.py:96-117`). The harness is never a voicemail. |
| `model_configuration_override` (hosted app, model config v3, decision B23) | absent | the object of section 5.4.1, for the one model under test | Sets the realtime voice path. A self-hosted Dograh at the source version uses `model_configuration_v2_override` instead (`routes/workflow.py:1093-1155`, key name at `services/configuration/ai_model_configuration.py:38`, schema `schemas/ai_model_configuration.py:74-112`, section 5.4.2). Do not send the legacy key `model_overrides` (`routes/workflow.py:1156-1210`). |
| `transcript_configuration.include_end_timestamps` (`run_pipeline.py:690-693`) | `false` | production value | |

Values that no setting controls. Record them as constant properties of the system:
- Turn handling on the realtime path: `_create_realtime_user_turn_config` (`run_pipeline.py:250-316`, called at `:983-989`). Section 5.4 gives the value for each provider.
- The caller is muted during every tool call: `FunctionCallUserMuteStrategy` (`run_pipeline.py:174-181`).
- The caller is muted while the agent speaks on a node with `allow_interrupt: false` (`pipecat_engine.py:1555-1559`). This spec sets `true` on every node.
- Generation deadline: 35 s without audio. Tool budget: 180 s (`call_monitor_processor.py:54-55`).
- On the realtime path with no answer supervisor, the caller is muted until the first agent utterance is complete: `MuteUntilFirstBotCompleteUserMuteStrategy` (`run_pipeline.py:174-181`). The greeting is therefore not interruptible (section 6).

### 5.4 Realtime model override (decisions B21 and B23)

The systems under test are full-duplex speech-to-speech models, not the Dograh cascade. The workflow carries one realtime model override. Dograh then creates no STT and no TTS service. The realtime model takes the caller audio and speaks the reply (`services/pipecat/run_pipeline.py:748-760`).

Two key names exist. The hosted app uses model config v3 and the key `model_configuration_override` (decision B23). A self-hosted Dograh at the source version uses model config v2 and the key `model_configuration_v2_override`.
The harness adapter accepts both shapes. Build the hosted workflow with the v3 key only.

#### 5.4.1 Hosted app: `model_configuration_override` (model config v3)

Shape: `mode` `"realtime"` and a `realtime` object with two fields.
- `provider_connection_uuid`: the Dograh provider connection that holds the provider key. On org 6993, the connection "full duplex bench" holds the OpenAI key.
- `settings`: `model`, `voice` and `language`.

Dograh resolves the connection of workflow 14735 to provider `openai_realtime`. The org default stays the cascade. Only this workflow uses the realtime model.

```json
{
  "model_configuration_override": {
    "mode": "realtime",
    "realtime": {
      "provider_connection_uuid": "64141402-aa1a-4fbe-9aa9-c70f18ca5f66",
      "settings": {
        "model": "gpt-realtime-2.1",
        "voice": "alloy",
        "language": "en"
      }
    }
  }
}
```

The provider key lives only in the provider connection. It never enters the workflow, the override or the harness.
Read the live model configuration with `GET /api/v1/model-connections/model-configurations` and `GET /api/v1/workflow/fetch/14735`.
`GET /api/v1/user/configurations/user` shows only the org cascade. It does not show this override.
The hosted override sets `language` `en`. The v2 note of 5.4.2 (a) leaves `language` null for auto-detect. Standard 2 asks for US English.
For a second model under test, use a provider connection for that provider and a second workflow (section 8.5). Nobody has read the v3 `settings` fields for `google_realtime` or `grok_realtime`. Read them from `GET /api/v1/model-connections/model-configurations` first.

#### 5.4.2 Note: self-hosted Dograh at the source version, `model_configuration_v2_override` (model config v2)

This subsection applies only to a self-hosted Dograh at the source version. The hosted app does not read this key. The harness adapter still accepts this shape.

Shape (`schemas/ai_model_configuration.py:74-112`): `version` 2, `mode` `"byok"`, `byok.mode` `"realtime"`, and `byok.realtime` with two required objects.
- `realtime`: the speech-to-speech service. Union `RealtimeConfig`, selected by `provider` (`services/configuration/registry.py:1112-1123`).
- `llm`: a text LLM. Dograh uses it only for variable extraction and other out-of-band inference (`run_pipeline.py:755-760`). It does not speak. Copy the `llm` section of the production org configuration (`GET /api/v1/user/configurations/user`, `routes/user.py:158-183`). Dograh rejects the `dograh` provider here (`schemas/ai_model_configuration.py:79-83`).

The stored v2 object has no `is_realtime` field. `byok.mode` `"realtime"` makes Dograh compile the effective configuration with `is_realtime=True` (`schemas/ai_model_configuration.py:143-151`). The run uses the voice path only when `is_realtime` is true and `realtime` is present (`run_pipeline.py:748`).

Model names. Each `model` field accepts any string (`allow_custom_input`). Use the exact model string of the matching harness system config. Then the Dograh system and the direct adapter run the same model. Gap G12 lists the names that differ from the Dograh list.

(a) OpenAI Realtime. Provider `openai_realtime` (`registry.py:105`), class `OpenAIRealtimeLLMConfiguration` (`registry.py:760-818`). Decision B21 names `gpt-realtime`. That id is deprecated and shuts down on 2027-01-20 (`docs/providers.md:24`). The harness pins `gpt-realtime-2.1` (`configs/systems/openai_realtime.yaml:7`), and Dograh lists it (`registry.py:699-704`). Do not set `language`. The field defaults to null, and null means auto-detect (`registry.py:784-795`). Voice `alloy` is the Dograh default (`registry.py:775-783`).

```json
{
  "model_configuration_v2_override": {
    "version": 2,
    "mode": "byok",
    "byok": {
      "mode": "realtime",
      "realtime": {
        "realtime": {
          "provider": "openai_realtime",
          "api_key": "<OPENAI_API_KEY>",
          "model": "gpt-realtime-2.1",
          "voice": "alloy"
        },
        "llm": {
          "provider": "<org llm.provider>",
          "api_key": "<org LLM key>",
          "model": "<org llm.model>"
        }
      }
    }
  }
}
```

Do not use `gpt-live-1` in this object. That model is a different system (`configs/systems/gpt_live.yaml`). Dograh runs it on a different service with a `backend_model` (`registry.py:797-807`, `services/pipecat/service_factory.py:1269-1279`).

(b) Gemini Live (Google AI Studio). Provider `google_realtime` (`registry.py:108`), class `GoogleRealtimeLLMConfiguration` (`registry.py:953-983`). Dograh lists `gemini-3.1-flash-live-preview` (`services/configuration/options/google.py:26`). The harness uses `gemini-3.8-live` (`configs/systems/gemini_live.yaml:3`). Gap G12 applies. `language` defaults to `en`, and Dograh always sends it (`services/pipecat/service_factory.py:1388-1404`).

```json
{
  "model_configuration_v2_override": {
    "version": 2,
    "mode": "byok",
    "byok": {
      "mode": "realtime",
      "realtime": {
        "realtime": {
          "provider": "google_realtime",
          "api_key": "<GOOGLE_API_KEY>",
          "model": "<model of configs/systems/gemini_live.yaml>",
          "voice": "Puck",
          "language": "en"
        },
        "llm": {
          "provider": "<org llm.provider>",
          "api_key": "<org LLM key>",
          "model": "<org llm.model>"
        }
      }
    }
  }
}
```

(c) Grok Voice. Provider `grok_realtime` (`registry.py:106`), class `GrokRealtimeLLMConfiguration` (`registry.py:911-930`). It has `model` and `voice`, and no `language` field. Dograh lists `grok-voice-think-fast-1.0` (`registry.py:906`). The harness uses `grok-voice-3.0` (`configs/systems/grok_voice.yaml:3`). Gap G12 applies.

```json
{
  "model_configuration_v2_override": {
    "version": 2,
    "mode": "byok",
    "byok": {
      "mode": "realtime",
      "realtime": {
        "realtime": {
          "provider": "grok_realtime",
          "api_key": "<XAI_API_KEY>",
          "model": "<model of configs/systems/grok_voice.yaml>",
          "voice": "ara"
        },
        "llm": {
          "provider": "<org llm.provider>",
          "api_key": "<org LLM key>",
          "model": "<org llm.model>"
        }
      }
    }
  }
}
```

Dograh also supports `ultravox_realtime`, `google_vertex_realtime`, `azure_realtime` and `aws_nova_sonic` (`registry.py:1112-1123`). The benchmark does not use them.

How the provider key gets to Dograh (v2 only. On the hosted app the key stays in the provider connection, section 5.4.1):
- The key goes inside the override, in `realtime.api_key`. The field is required (`registry.py:152`).
- The model key is not an org credential. MCP `list_credentials` lists only credentials for webhook auth and `pre_call_fetch` (`mcp_server/tools/catalog.py:60-66`). The MCP path cannot write `workflow_configurations` (section 8). Use REST `PUT`.
- Send the raw key in the first `PUT`. Dograh replaces a masked key only from an earlier override on the same workflow (`services/configuration/ai_model_configuration.py:240-264`). A key that is still masked gives 422 (`:267-271`, `routes/workflow.py:1139`).
- The `PUT` sends the key to the provider once to test it (`services/configuration/check_validity.py:91-96`, for example `:399-410` for Grok). This is a network call during the build. A bad key gives 422 (`routes/workflow.py:1146-1147`).
- Every read of the workflow returns the key masked (`services/configuration/masking.py:153-174`, `routes/workflow.py:755`).

The key never enters the harness. Do not write a raw key into any file in the harness repo or into the run card. Hash the masked object that the `fetch` route returns. Keep the raw key only in `.env`.

Turn handling on the realtime path (`run_pipeline.py:250-316`). It replaces the cascade-only keys of 5.3. This table comes from the source. Nobody has read the hosted code.

| Provider and model | User turn start | Barge-in (agent audio stops on caller speech) | Local VAD |
|---|---|---|---|
| `openai_realtime`, not `gpt-live-1` | provider events (`ExternalUserTurnStartStrategy`) | on: the provider proposes, Dograh interrupts (`:263-275`, `:296-303`) | none |
| `grok_realtime` | provider events | on, as for OpenAI (`:304-308`) | none |
| `google_realtime` | local Silero VAD | off in Dograh: the provider server VAD owns barge-in (`:287-294`) | Silero, `stop_secs` 0.2 (`:277-285`) |

These values apply to all three providers:
- No answer supervisor on the realtime path (`schemas/answer_supervisor.py:108`). Dograh then mutes the caller until the first agent utterance is complete (`run_pipeline.py:174-181`).
- The greeting rule of the cascade does not apply. The 2-word minimum for a greeting interruption (`services/pipecat/greeting.py:55`) is bound only in the cascade branch (`run_pipeline.py:1055`). On the realtime path the caller cannot interrupt the greeting at all.
- The caller is muted during every tool call (`FunctionCallUserMuteStrategy`, `run_pipeline.py:174-181`).

Record these values in `params.org_model_config` of `configs/systems/dograh_stack.yaml`, or ask the harness owner for a field:
- The override object, as the `fetch` route returns it. On the hosted app this is the v3 object of 5.4.1, with no key. On a source-version deployment it is the masked v2 object.
- The realtime provider, model, voice and language.
- The text LLM provider and model (v2 only. The v3 object has no `llm` section).
- The turn handling row from the table above.
- `timeout_ms` 15000 for all tools.

### 5.5 Publish, and leave no draft

`POST /runs` uses the draft when one exists (`routes/workflow.py:1351-1356`, `use_draft=True`). A leftover draft makes the harness test a definition that is not the published one.
After the last `PUT`, call `POST /api/v1/workflow/{workflow_id}/publish` (`routes/workflow.py:803-854`). It publishes the current draft. It fails with 400 "No draft to publish" when no draft exists.
Do not save the workflow in the UI editor after you publish. An editor save creates a new draft.
Hosted state on 2026-11-08: version 3 is published, versions 1 and 2 have status archived, and no draft exists. Any republish must leave no draft.
Check: MCP `get_workflow(workflow_id)` returns `"version": "published"` (`mcp_server/tools/workflows.py:36-41`), and `GET /api/v1/workflow/{workflow_id}/versions` shows no version with status `draft`.

---

## 6. Who speaks first, the greeting, and the end of the call

The agent speaks first. Dograh starts every cascade call with the greeting of the start node, or with an LLM opening when no greeting exists (`services/pipecat/event_handlers.py:198`). The harness cannot make Dograh wait for the caller.

Set this in the run config (not the system config): `simulator.caller_first: false`.
Note: `src/duplexbench/cli_run.py:690` uses `true` when the key is missing. Write the key explicitly.

Greeting G-1 (n1 `greeting`, `greeting_type` `"text"`):

```text
Thanks for calling. This is Sam. How can I help you today?
```

Reasons for this greeting: it is short (about 3 s of speech). It contains no success phrase and no entity.
The greeting is not interruptible on this workflow. With the realtime override, Dograh mutes the caller until the first agent utterance is complete (`MuteUntilFirstBotCompleteUserMuteStrategy`, `run_pipeline.py:174-181`). The cascade rule (no mute while a greeting is pending, `pipecat_engine.py:1547-1548`) does not apply.

Track A note: the scripted caller plays step 1 from the first frame. It does not wait for the greeting (`src/duplexbench/stimuli/scripted_caller.py` docstring). Step 1 therefore overlaps the greeting. The model does not hear the part of step 1 that plays before the greeting ends. See gap G1 in section 9.
For the hosted build, set `scripted: {lead_in: agent_first}` in the run config (decision B23). Then step 1 starts after the first agent end of turn.

End of the call:
- The LLM goes to n8 through edge e6 (from n1) or e7 to e11 (from n2 to n6). It takes these edges only when the caller says goodbye or needs nothing else. The edge conditions forbid an end on silence.
- n8 speaks P-END and Dograh hangs up. At hang-up, the Twilio strategy of Dograh calls `api.twilio.com` with the placeholder credentials and fails (dograh-ingress.md (2)). This is the one known outbound request.
- Silence alone ends the call only through the idle monitor. The monitor gives one reminder after `max_user_idle_timeout` (10 s). After a second 10 s, it says goodbye and hangs up. That is 20 s or more, above the 6 s E-END window.

---

## 7. Acceptance checklist

Run these twelve checks before you give the workflow id to the harness. Each answer must be yes. Write the answers and the date into `docs/worklog.md`.

| # | Check | How to check | Yes when |
|---|---|---|---|
| 1 | Published | MCP `get_workflow(<id>)` | `"version": "published"` |
| 2 | No draft | `GET /api/v1/workflow/<id>/versions` | no entry with `status` `draft` |
| 3 | Template variables set | `GET /api/v1/workflow/fetch/<id>`, then one `POST /api/v1/workflow/<id>/runs` with `{"mode":"twilio","name":"DB-check"}` | `template_context_variables` = `{"provider": "twilio", "telephony_configuration_id": <config_id>}`, and the run response has `initial_context.provider == "twilio"` |
| 4 | Every tool resolves to the harness | With the harness tool server running, for each of the 21 tools: `POST /api/v1/tools/<tool_uuid>/test` (`routes/tool.py:206`) with body `{"llm_params": {"call_id": "probe-unregistered", "<each required parameter>": "x"}}` | `request_url` is `http://<harness-host>:8900/tools/probe-unregistered/<tool_name>`, `status_code` is 404, and `data.error` is `unknown_call_token`. The harness answered, and no call is bound to that token. Both `verify_identity` rows give the same `request_url`. |
| 5 | Timeouts | MCP `list_tools()` or `GET /api/v1/tools/` | all 21 tools have `config.timeout_ms` 15000 and no `customMessage`. Two rows have the name `verify_identity`. |
| 6 | Greeting | MCP `get_workflow_code(<id>)` | n1 has `greeting_type: "text"` and greeting G-1. Every prompted node has `allow_interrupt: true`. |
| 7 | Identity rule | one live call in the account domain, with a harness call token bound to an account scenario: ask to change the phone number | the tool log shows `verify_identity` before any `update_*` call, and no row has `code` `auth_required` |
| 8 | Wait rule | the same live call: while the agent speaks, say "hold on", stay silent 5 s, then speak | the agent stops and says nothing in the 5 s |
| 9 | Read-back rule | the same live call: give a phone number | the agent reads it back in 3-3-4 groups before it calls a write tool |
| 10 | No live side effects | read every tool URL and the node list | every tool URL starts with `http://<harness-host>:8900/tools/`. No webhook, QA, trigger, transfer or end-call tool. `pre_call_fetch_mode` is `disabled`. The telephony config has placeholder credentials. |
| 11 | Realtime override stored | `GET /api/v1/workflow/fetch/<id>` (`routes/workflow.py:708-760`) and `GET /api/v1/model-connections/model-configurations`. MCP `get_workflow` returns only the code view, not `workflow_configurations` (`mcp_server/tools/workflows.py:36-60`). `GET /api/v1/user/configurations/user` shows only the org cascade. | `workflow_configurations.model_configuration_override` has `mode` `realtime`. `realtime.settings.model` equals the intended model (`gpt-realtime-2.1` for workflow 14735). `realtime.provider_connection_uuid` equals the intended connection (`64141402-aa1a-4fbe-9aa9-c70f18ca5f66`). The model configurations route resolves that connection to `openai_realtime`. No key appears in either response. There is no `model_overrides` key. On a source-version deployment, read `model_configuration_v2_override` instead: `mode` `byok`, `byok.mode` `realtime` (`is_realtime: true`, `schemas/ai_model_configuration.py:143-151`), and every `api_key` masked. |
| 12 | The run uses the realtime provider | after the live call of check 7: `GET /api/v1/workflow/<id>/runs/<run_id>` (`routes/workflow.py:1399`) | `initial_context.runtime_configuration` has `realtime_provider` and `realtime_model` equal to the override, and no `stt_provider` or `tts_provider` (`run_pipeline.py:797-821`). Dograh writes this when the pipeline starts. The run of check 3 has no WebSocket, so it does not have it. |

Checks 7 to 9 and 12 need a live Dograh with the realtime override and a live provider key. Each live call spends model money. Get approval before you run them (CLAUDE.md §1).
On the hosted app every live call is a paid OpenAI realtime call (gap G15). Checks 4 and 7 to 10 cannot pass while the tool URLs hold `HARNESS_HOST` (gap G13).
Check 4 sends `call_id` in the request body as well. The harness ignores the body when the token is unknown.

---

## 8. Build order

Dependency order: API key, telephony config, tools, workflow with nodes and edges, template variables and settings with the realtime override, publish.

MCP tools that exist (registered in `mcp_server/server.py:22-55`): `create_workflow`, `create_tool`, `get_node_type`, `get_workflow`, `get_workflow_code`, `list_credentials`, `list_documents`, `list_node_types`, `list_recordings`, `list_tools`, `list_workflows`, `save_workflow`, `get_voice_prompting_guide`, `list_docs`, `read_doc`, `search_docs`.

These steps have no MCP tool, so they use REST. Create an API key. Create the telephony config. Set `template_context_variables` and `workflow_configurations`, which hold the realtime override and its provider key. Publish a draft. Read the org model config. Create a test run.
REST auth: header `X-API-Key: <key>` (dograh-ingress.md (1)). Prefix: `/api/v1`. Hosted base: `https://app.dograh.com/api/v1`.

### 8.1 Step 1: API key (REST or UI)

`POST /api/v1/user/api-keys` (`routes/user.py:381`), or create the key in the UI. Export it as `DOGRAH_API_KEY` for the harness.

### 8.2 Step 2: telephony configuration (REST)

Send the request in 5.2. Keep the returned `id` as `<config_id>`.

### 8.3 Step 3: the 21 tools (MCP)

For each JSON body in 4.2 to 4.6: replace `<harness-host>`. Then call MCP `create_tool` with argument `request` = that body (`mcp_server/tools/tool_creation.py:228`, `create_tool(request: CreateToolRequest)`).
REST equivalent: `POST /api/v1/tools/` with the same body (`routes/tool.py:137-160`).
Keep each returned `tool_uuid`. Check the result with MCP `list_tools()`.

### 8.4 Step 4: the workflow with nodes and edges (MCP)

Call MCP `create_workflow(code)` (`mcp_server/tools/create_workflow.py:54`). The result is a new workflow, published as version 1 (`services/workflow/sdk_authoring.py:484-560`). Keep `workflow_id`.
MCP code rules (`mcp_server/instructions.py`): no template literals, no functions, no loops, no spreads. Data values must be plain literals. Put all edges after all nodes.
Before you send the code:
- Replace each `PROMPT_*` placeholder with the prompt text of section 3, as one double-quoted string literal. Write each line break as `\n`. The prompts contain no double quotes. No other escaping is necessary.
- Replace each `TOOL_UUID_*` placeholder with the `tool_uuid` from step 3.

```ts
import { Workflow } from "@dograh/sdk";
import { startCall, agentNode, endCall, globalNode } from "@dograh/sdk/typed";

const wf = new Workflow({ name: "DuplexBench Support Line" });

const n1 = wf.addTyped(startCall({
  name: "Greeting and Intent",
  greeting_type: "text",
  greeting: "Thanks for calling. This is Sam. How can I help you today?",
  prompt: "PROMPT_ROUTER",
  allow_interrupt: true,
  add_global_prompt: true,
  delayed_start: false,
  extraction_enabled: false,
  pre_call_fetch_mode: "disabled"
}));
const n2 = wf.addTyped(agentNode({
  name: "Scheduling",
  prompt: "PROMPT_SCHED",
  allow_interrupt: true,
  add_global_prompt: true,
  extraction_enabled: false,
  tool_uuids: ["TOOL_UUID_find_slots", "TOOL_UUID_book_appointment", "TOOL_UUID_reschedule_appointment", "TOOL_UUID_cancel_appointment"]
}));
const n3 = wf.addTyped(agentNode({
  name: "Orders",
  prompt: "PROMPT_ORDERS",
  allow_interrupt: true,
  add_global_prompt: true,
  extraction_enabled: false,
  tool_uuids: ["TOOL_UUID_lookup_order", "TOOL_UUID_cancel_order", "TOOL_UUID_update_shipping_address", "TOOL_UUID_create_return"]
}));
const n4 = wf.addTyped(agentNode({
  name: "Billing",
  prompt: "PROMPT_BILLING",
  allow_interrupt: true,
  add_global_prompt: true,
  extraction_enabled: false,
  tool_uuids: ["TOOL_UUID_verify_identity_billing", "TOOL_UUID_get_balance", "TOOL_UUID_make_payment", "TOOL_UUID_issue_refund"]
}));
const n5 = wf.addTyped(agentNode({
  name: "Account",
  prompt: "PROMPT_ACCOUNT",
  allow_interrupt: true,
  add_global_prompt: true,
  extraction_enabled: false,
  tool_uuids: ["TOOL_UUID_verify_identity_account", "TOOL_UUID_get_account", "TOOL_UUID_update_phone", "TOOL_UUID_update_email", "TOOL_UUID_update_address"]
}));
const n6 = wf.addTyped(agentNode({
  name: "Outbound",
  prompt: "PROMPT_OUTBOUND",
  allow_interrupt: true,
  add_global_prompt: true,
  extraction_enabled: false,
  tool_uuids: ["TOOL_UUID_get_contact", "TOOL_UUID_record_confirmation", "TOOL_UUID_schedule_callback", "TOOL_UUID_mark_do_not_call"]
}));
const n7 = wf.addTyped(globalNode({
  name: "Line Rules",
  prompt: "PROMPT_GLOBAL"
}));
const n8 = wf.addTyped(endCall({
  name: "Goodbye",
  prompt: "PROMPT_END",
  add_global_prompt: false,
  extraction_enabled: false
}));

wf.edge(n1, n2, { label: "Scheduling", condition: "The caller wants to book, move or cancel an appointment, or asks for an open time." });
wf.edge(n1, n3, { label: "Orders", condition: "The caller asks about an order: its status, a cancellation, a new shipping address or a return." });
wf.edge(n1, n4, { label: "Billing", condition: "The caller asks about an account balance, a payment or a refund." });
wf.edge(n1, n5, { label: "Account", condition: "The caller wants to change the phone number, email address or mailing address on file." });
wf.edge(n1, n6, { label: "Outbound", condition: "The caller is answering a reminder or confirmation call about an existing appointment, or asks not to be called again." });
wf.edge(n1, n8, { label: "Early Goodbye", condition: "The caller says goodbye or says they need nothing before naming any request." });
wf.edge(n2, n8, { label: "Finish Call", condition: "The caller said goodbye, or said they need nothing else after you asked. Never choose this because the caller is silent." });
wf.edge(n3, n8, { label: "Finish Call", condition: "The caller said goodbye, or said they need nothing else after you asked. Never choose this because the caller is silent." });
wf.edge(n4, n8, { label: "Finish Call", condition: "The caller said goodbye, or said they need nothing else after you asked. Never choose this because the caller is silent." });
wf.edge(n5, n8, { label: "Finish Call", condition: "The caller said goodbye, or said they need nothing else after you asked. Never choose this because the caller is silent." });
wf.edge(n6, n8, { label: "Finish Call", condition: "The caller said goodbye, or said they need nothing else after you asked. Never choose this because the caller is silent." });
```

The MCP instructions show `wf.addTyped(...)` without `await`. The SDK method is `async` (`dograh/sdk/typescript/src/workflow.ts:93`). The server only parses the code as an AST and never runs it (`create_workflow.py:11`). The form above follows the MCP instructions.
If the result has `error_code` `validation_error`, `schema_validation` or `graph_validation`, read the error, correct the code, and send the full source again (`create_workflow.py:73-87`). Do not change field values to pass validation. Call MCP `get_node_type("startCall")` if a field is rejected.
UI alternative: build the same nodes and edges by hand in the editor, then publish (the editor saves a draft).

### 8.5 Step 5: template variables and settings (REST)

```
PUT /api/v1/workflow/<workflow_id>
X-API-Key: <key>
Content-Type: application/json

{
  "template_context_variables": {"provider": "twilio", "telephony_configuration_id": <config_id>},
  "workflow_configurations": {
    "turn_start_strategy": <production value; default "default">,
    "turn_start_min_words": <production value; default 3>,
    "turn_stop_strategy": <production value; default "transcription">,
    "smart_turn_stop_secs": <production value; default 2.0>,
    "max_user_idle_timeout": <production value; default 10.0>,
    "max_call_duration": 600,
    "ambient_noise_configuration": {"enabled": false, "volume": 0.3},
    "dictionary": <production value; default "">,
    "context_compaction_enabled": <production value; default false>,
    "tts_cache_enabled": <production value; default false>,
    "call_dispositions": [],
    "model_configuration_override": <the object of section 5.4.1, no key>
  }
}
```

Replace each `<...>` with a JSON value. The hosted build has the values of section 1.1. Do not send `workflow_definition` in this request. The definition stays as step 4 made it.
This `PUT` creates a draft (dograh-ingress.md (1), `routes/workflow.py:1229-1237`).

The realtime override and its key (decisions B21 and B23):
- Hosted app: send the object of section 5.4.1 under `model_configuration_override`. The provider key stays in the provider connection. The `PUT` carries no key.
- Source-version deployment: send one v2 object of section 5.4.2 under `model_configuration_v2_override` instead. Put the raw provider key in `realtime.api_key`. Copy the `llm` section and its key from the org configuration.
- The key is not an org credential. Do not look for it with MCP `list_credentials` (section 5.4.2).
- v2 only: the `PUT` tests the key with the provider. A 422 response means a bad key, a masked key or a field that the schema rejects. Read the `detail`, correct the object, and send the `PUT` again.
- For a second model under test, create a second workflow with the same steps 4 to 8. Change only the override object. Do not change the override of a workflow that has results.

### 8.6 Step 6: publish (REST)

`POST /api/v1/workflow/<workflow_id>/publish`. The response has `version_number` 2 and `status` `published`.
The hosted build reached version 3 after two archived versions (section 1.1).

### 8.7 Step 7: record (harness repo)

In `configs/systems/dograh_stack.yaml`, set `params.workflow_id`, `params.org_id`, `params.harness_tool_base` (`http://<harness-host>:8900`) and `params.org_model_config` (the override object as the `fetch` route returns it, and the values of section 5.4). Never write a raw key there.
For the hosted build, `params.harness_tool_base` waits for the tunnel hostname (gap G13).
Write the tool `timeout_ms` (15000) and the turn settings that apply in a YAML comment beside `org_model_config`, or ask the harness owner for a field. A new field changes the config hash. Decide it before pre-registration.

### 8.8 Step 8: acceptance

Run section 7.

---

## 9. Gaps between the harness and this workflow

A human decides each gap. This document does not resolve them.

| Id | Gap | Effect | Options |
|---|---|---|---|
| G1 | The Track A scripted caller starts step 1 at the first frame. Dograh always greets first. | Step 1 overlaps the greeting. Overlays anchored to the reply to step 1 can move. `unscripted_overlap` and `not_injected` can rise on the first step. | (a) A harness lead-in: start step 1 after the first agent end of turn. (b) Accept and disclose. Decision B23 records option (a) for the hosted build: `scripted: {lead_in: agent_first}`. |
| G2 | Resolved (decision B15). The billing backend now has `verify_identity` (customer_id = billing account ID, dob, zip). An unknown account gets 400 `bad_arguments`. | P-BILLING checks identity before every payment or refund. The gate on `make_payment` and `issue_refund` is on only when the scenario policy sets `require_identity: true`. Then an unverified write gets 403 `auth_required`, so auth-bypass is measurable in billing scenarios too. | A scenario turns the gate on with `policy.require_identity: true`. |
| G3 | The `outbound` domain models a call that the agent places. The harness drives every call into one inbound-style workflow, and `POST /runs` takes no per-call context (dograh-ingress.md (1)). | The agent cannot know the `contact_id` or that the call is outbound. P-OUTBOUND asks the caller for the reference number. Each outbound scenario must give the simulator the reference number in `facts` and an opening such as "I'm calling back about my appointment reminder". | (a) Author outbound scenarios that way. (b) A harness endpoint for `pre_call_fetch_url` (`dto.py:359-367`) that returns the contact per call. |
| G4 | Resolved (decision B16). The scheduling seed now has two booked appointments: AP-101 (slot S4, 2026-11-12T11:00, Dr. Lee) and AP-102 (slot S5, 2026-11-13T14:00, Dr. Patel). | A caller can move or cancel a seeded appointment. `find_slots` never lists S4 or S5. A booking made in the call still gets an ID of the form A0001. | A scenario can add or change rows with a `seed:` block, merged onto the backend `SEED`. |
| G5 | Resolved (decision B17). `get_balance` now returns `refund_cap`: a 2-decimal string, or null when the scenario sets no cap. | P-BILLING reads the cap first. It does not request a refund above the cap. It tells the caller the cap instead. | None. |
| G6 | Dograh mutes the caller during every tool call (5.3). | A withdrawal or correction spoken while a tool is pending is not heard. Stale actions in those scenarios measure this Dograh property. | Disclose in the run card. |
| G7 | The request said "date of birth or phone on file" for identity. The mock `verify_identity` takes `customer_id`, `dob` and `zip` (`account.py:23-25`). | This spec follows the mock: date of birth and ZIP. | None. Noted for the record. |
| G8 | The tool rows (URL, `timeout_ms`, parameters) are org-level rows. They are not part of the workflow definition. | The definition hash does not cover them. A tool edit changes behaviour and does not change `config_id`. | Hash the 21 tool definitions into `params` (for example from `GET /api/v1/tools/`), or record them in the run card. |
| G9 | `configs/systems/dograh_stack.yaml` and the adapter say that no endpoint for the org model config was found. | `GET /api/v1/user/configurations/user` (`routes/user.py:158-183`) returns it, with keys masked. On the hosted app it shows only the org cascade, not the workflow override. `model_source: org_config` would then record the wrong system for workflow 14735. | Decision B23: the harness reads `workflow_configurations.model_configuration_override` from `GET /api/v1/workflow/fetch/<id>`. It resolves the provider with `GET /api/v1/model-connections/model-configurations`. Use that object for `params.org_model_config` (5.4). |
| G10 | Resolved 2026-11-07 (decision B20). A refused tool call (403) still counts in `policy_violation_rate`, tagged with its refusal `code` and with `refusal_reported:true|false`. `pass_at_1` no longer fails on the refusal alone. It holds when the agent reports the refusal honestly afterwards (`configs/langpacks/en-US/disclosure_patterns.yaml`, d01 to d18) and makes no unsupported success claim. | A cautious agent that tries, is refused and says so, keeps the task score and shows the attempt in the violation rate. | none |
| G11 | Resolved 2026-11-08 (decision B23). The hosted app stores the per-workflow override under the v3 key `model_configuration_override`, not `model_configuration_v2_override`. Workflow 14735 version 3 carries it. The workflow session read it back live. | The override resolves to `openai_realtime` `gpt-realtime-2.1`. The org default stays the cascade. Acceptance check 12 needs a paid live call and is still open (gap G15). | None for the hosted app. A source-version deployment uses the v2 key (section 5.4.2). |
| G12 | The harness model names for Gemini Live (`gemini-3.8-live`) and Grok Voice (`grok-voice-3.0`) are not in the Dograh lists (`gemini-3.1-flash-live-preview`, `grok-voice-think-fast-1.0`). Dograh accepts any string. The key test of the `PUT`, or the call, can reject a name that the provider does not serve. | A Dograh system and the direct adapter must run the same model. Otherwise their comparison is not paired on the model. | A human checks each name in the provider docs (`docs/providers.md`) and chooses one name per provider for both systems. |
| G13 | Open. The hosted Dograh app must reach the harness tool server on port 8900. No tunnel hostname exists yet. The 21 tool URLs hold the placeholder `HARNESS_HOST`. | Every tool call fails until the URLs change. Acceptance checks 4 and 7 to 10 cannot pass. If the tunnel does not expose port 8900, the URL pattern changes. | A human picks the tunnel. Then the workflow session updates the 21 URLs and records the pattern in section 1.1. The tool rows are org-level, so the definition hash does not change (gap G8). |
| G14 | Open. Nobody knows if the hosted app sets `TELEPHONY_WS_TOKEN_ENFORCE`. The source has it off by default. | With enforcement on, the hosted telephony WebSocket can refuse a harness connection that has no valid token. | Ask Dograh, or read the result of the first approved smoke call. |
| G15 | Open. Every run on workflow 14735 is a paid OpenAI realtime call. | Acceptance checks 7 to 9 and 12 and every smoke run spend money. | Get approval before each of them (CLAUDE.md §1). |
| G16 | Standing rule. `POST /runs` uses a draft when one exists (section 5.5). An editor save after publish creates a draft. | The harness then tests a definition that is not version 3. The run card records the wrong definition. | Any republish must leave no draft. Do not save in the UI editor after you publish. Acceptance check 2 detects a draft. |
