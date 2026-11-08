"""Dograh stack adapter: harness as the Twilio carrier (BUILD_PLAN M6-T02, decision D2).

Every step comes from docs/dograh-ingress.md (static reading of Dograh at cb7f614;
FALLBACK B not required, so `components_only=False`):

1. `pin_definition(wf)`: `GET /api/v1/workflow/fetch/{wf}` -> `current_definition_id`,
   `GET /api/v1/workflow/{wf}/versions` -> that version's `workflow_json`,
   `workflow_configurations`, `template_context_variables`. Dograh keeps no live
   config hash (`workflow_hash` is legacy), so the harness hashes the pinned
   definition plus two things that live outside it (WORKFLOW_SPEC 9, G8 / G9):
   - `tool_definitions` (fix-35): `GET /api/v1/tools/` (Dograh routes/tool.py:96-134,
     `list[ToolResponse]`, schemas/tool.py:683-697). Only the rows whose `tool_uuid`
     appears in a `tool_uuids` list of the pinned `workflow_json` are hashed, and only
     their behavioural fields (`TOOL_ROW_FIELDS`; header names, never values; a
     credential reference becomes a presence flag). A failed fetch hashes nothing for
     tools and adds the disclosure "tool definitions not hashed: <reason>".
   - `org_model_config` (fix-36): Dograh ignores the org configuration whenever
     `workflow_configurations.model_configuration_v2_override` is set
     (services/configuration/ai_model_configuration.py:85-97); that override is
     already hashed, so `model_source = "workflow_override"` and nothing is fetched.
     Otherwise `GET /api/v1/user/configurations/user` (routes/user.py:158-183, masked
     by services/configuration/masking.py:138-150) is hashed without its secret,
     phone and pricing fields (`model_source = "org_config"`). When that fetch fails
     (the hosted app answers 404, G11) the YAML `params.org_model_config` is hashed
     if pasted (`model_source = "pasted"`), else nothing (`model_source = "unknown"`),
     each with a disclosure.
   - fix-43 (decision B23): the hosted app (model config v3) keeps the realtime override
     under `workflow_configurations.model_configuration_override`
     {"mode": "realtime", "realtime": {"provider_connection_uuid", "settings": {model,
     voice, language}}}. It is preferred over the v2 key when both are set (disclosed).
     Its provider is resolved by uuid from `GET /api/v1/model-connections/model-configurations`
     (hosted only; the local Dograh source at cb7f614 predates it, so the response is read
     defensively: any object carrying that uuid and a `provider` string). A failed lookup
     records `provider: "unresolved:<uuid>"` and a disclosure. The connection row is
     outside the hashed definition (only its uuid is hashed).
2. Per call: `POST /api/v1/workflow/{wf}/runs` `{"mode":"twilio","name":...}` with
   header `X-API-Key` -> `id`, `definition_id`, `initial_context` (must carry
   `provider == "twilio"`, from the workflow's template-context variables).
3. WebSocket `ws(s)://<host>/api/v1/telephony/ws/{wf}/{org}/{run}[/{token}]`,
   token = hex HMAC-SHA256(secret, "{wf}:{org}:{run}") when the deployment sets
   `TELEPHONY_WS_TOKEN_SECRET` (optional).
4. Handshake `connected`, then `start{streamSid, callSid}`; callSid = the call's
   harness tool token, so Dograh's org-level `http_api` tools whose URL is
   `http://<harness>:<port>/tools/{{call_id}}/<tool>` reach this call's ToolServer
   binding. Tool calls therefore arrive at the ToolServer, never on this socket
   (`tool_events` hook = ToolServer.subscribe). `needs_http_tools = True`: `cli run`
   serves the ToolServer app on 0.0.0.0:<port of harness_tool_base> for the whole run
   (tools/http.py, fix-44); the hosted app must reach that port (security group / tunnel).
5. Inbound `media` -> audio, `clear` -> cancel_or_truncate, everything else logged.
   Dograh never sends `mark`.

Run-card disclosures (dograh-ingress.md (1), (2)): `POST /runs` takes no concurrency
slot and runs no quota check; the run uses the draft definition if one exists; at
call end pipecat's Twilio hang-up strategy POSTs to api.twilio.com with placeholder
credentials and fails (logged by Dograh, not billed).
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from duplexbench.adapters._convert import params_of
from duplexbench.transport.live_stream import Connector
from duplexbench.adapters.pipecat_cascade import ToolEvents, TwilioAgentAdapter
from duplexbench.storage import canonical_json

if TYPE_CHECKING:
    from duplexbench.clock import Clock
    from duplexbench.context import CallContext

API = "/api/v1"
API_KEY_HEADER = "X-API-Key"
DEFAULT_API_KEY_ENV = "DOGRAH_API_KEY"
REQUIRED_PARAMS = ("endpoint", "workflow_id", "org_id")
PLACEHOLDER = "TO_SET"         # configs/systems/dograh_stack.yaml ships ids as this placeholder
HASHED_FIELDS = ("workflow_json", "workflow_configurations", "template_context_variables")
TOOLS_PATH = "/tools/"                          # Dograh routes/tool.py:48,96 (list_tools)
ORG_MODEL_CONFIG_PATH = "/user/configurations/user"   # Dograh routes/user.py:50,158
OVERRIDE_KEY = "model_configuration_v2_override"      # ai_model_configuration.py:38
V3_OVERRIDE_KEY = "model_configuration_override"      # hosted model config v3 (B23)
MODEL_CONNECTIONS_PATH = "/model-connections/model-configurations"   # hosted (B23); not in source
CONNECTION_UUID_KEYS = ("provider_connection_uuid", "connection_uuid", "uuid")
# ToolResponse (schemas/tool.py:683-697) fields that change what the agent does. Dropped:
# id, tool_uuid, created_at, updated_at, created_by, icon, icon_color. `status` stays:
# Dograh loads only active tools at run time (db/tool_client.py:294-317).
TOOL_ROW_FIELDS = ("name", "description", "category", "status", "definition")
# Keys inside a tool definition / model config that carry secrets: never hashed.
# `credential_uuid` (an id) is replaced by a presence flag (`credential: true|false`).
SECRET_KEYS = frozenset({"api_key", "credentials", "aws_access_key", "aws_secret_key",
                         "aws_session_token", "token", "secret", "password",
                         "authorization", "qa_api_key"})
# GET /user/configurations/user keys that are not model behaviour (routes/user.py:166-181).
ORG_CONFIG_DROP = frozenset({"test_phone_number", "organization_pricing"})
DISCLOSURES = (
    "Runs are created with POST /api/v1/workflow/{wf}/runs (mode=twilio): no concurrency slot "
    "is taken and no quota check runs, unlike a production call (dograh-ingress.md (1)).",
    "POST /runs uses the draft definition when one exists; definition_id is recorded per call "
    "and compared with the pinned definition.",
    "At call end Dograh's Twilio hang-up strategy calls api.twilio.com with placeholder "
    "credentials and fails (logged side effect, not a billed call).",
    "Dograh sends no Twilio `mark`; playout timing comes from received media frames.",
    # fix-37 (WORKFLOW_SPEC 5.3, gap G6)
    "Dograh mutes the caller during every tool call (FunctionCallUserMuteStrategy, "
    "services/pipecat/run_pipeline.py:174-181): a correction or withdrawal spoken while a "
    "tool call is pending is not heard by the agent.",
    "On the realtime path with no answer supervisor, Dograh mutes the caller until the first "
    "agent utterance is complete (MuteUntilFirstBotCompleteUserMuteStrategy, "
    "services/pipecat/run_pipeline.py:174-181): the greeting cannot be interrupted.",
)


class DograhSetupError(RuntimeError):
    """The Dograh workflow or org is not set up as dograh-ingress.md's script requires."""


def mint_token(secret: str, workflow_id: Any, org_id: Any, run_id: Any) -> str:
    """Dograh `ws_auth.mint_ws_token`: hex HMAC-SHA256 over "{wf}:{org}:{run}"."""
    msg = f"{workflow_id}:{org_id}:{run_id}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def definition_hash(version: dict, org_model_config: dict | None = None, *,
                    tool_definitions: dict | None = None) -> str:
    """sha256 of the canonical JSON of the pinned definition's behavioural snapshot.

    `tool_definitions` (fix-35) enters the snapshot only when given, so a definition
    without hashed tools keeps the hash it had before fix-35."""
    snap = {k: version.get(k) for k in HASHED_FIELDS}
    snap["org_model_config"] = org_model_config
    if tool_definitions is not None:
        snap["tool_definitions"] = tool_definitions
    return hashlib.sha256(canonical_json(snap).encode()).hexdigest()


def referenced_tool_uuids(workflow_json: Any) -> list[str]:
    """Every string in any `tool_uuids` list of the workflow graph, sorted, unique."""
    found: set[str] = set()

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "tool_uuids" and isinstance(v, list):
                    found.update(x for x in v if isinstance(x, str))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(workflow_json)
    return sorted(found)


def _scrub(o: Any) -> Any:
    """Drop secret values; header dicts -> sorted names; credential_uuid -> presence."""
    if isinstance(o, dict):
        out: dict[str, Any] = {}
        for k, v in o.items():
            lk = str(k).lower()
            if lk in SECRET_KEYS:
                continue
            if lk == "credential_uuid":
                out["credential"] = bool(v)
            elif lk == "headers":
                out["header_names"] = sorted(v) if isinstance(v, dict) else None
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(o, list):
        return [_scrub(x) for x in o]
    return o


def tool_snapshot(rows: list[dict], refs: list[str]) -> dict:
    """The hashed form of the referenced org tool rows (fix-35)."""
    by_uuid = {r.get("tool_uuid"): r for r in rows if isinstance(r, dict)}
    picked = [{k: _scrub(by_uuid[u].get(k)) for k in TOOL_ROW_FIELDS}
              for u in refs if u in by_uuid]
    picked.sort(key=canonical_json)       # ids are dropped, so order by content
    return {"rows": picked, "missing": sum(1 for u in refs if u not in by_uuid)}


def org_config_snapshot(cfg: dict) -> dict:
    """GET /user/configurations/user without secrets, test phone and pricing (fix-36)."""
    return _scrub({k: v for k, v in cfg.items() if k not in ORG_CONFIG_DROP})


def override_models(override: Any) -> dict:
    """Provider / model names of a v2 override, never keys (schemas/ai_model_configuration.py:52-112)."""
    if not isinstance(override, dict):
        return {}
    out: dict[str, Any] = {"mode": override.get("mode")}
    byok = override.get("byok")
    if isinstance(byok, dict):
        out["byok_mode"] = byok.get("mode")
        sel = byok.get(byok.get("mode")) if isinstance(byok.get("mode"), str) else None
        if isinstance(sel, dict):
            for sec, svc in sorted(sel.items()):
                if isinstance(svc, dict):
                    out[sec] = {"provider": svc.get("provider"), "model": svc.get("model")}
    return out


def find_connection_provider(body: Any, conn_uuid: str) -> str | None:
    """`provider` of the first object in `body` whose uuid field equals `conn_uuid`.

    Shape-agnostic on purpose: the model-connections route is not in the local Dograh
    source, so no list-vs-object layout is assumed (fix-43)."""
    if isinstance(body, dict):
        if any(body.get(k) == conn_uuid for k in CONNECTION_UUID_KEYS) \
                and isinstance(body.get("provider"), str):
            return body["provider"]
        items = list(body.values())
    elif isinstance(body, list):
        items = body
    else:
        return None
    for x in items:
        hit = find_connection_provider(x, conn_uuid)
        if hit is not None:
            return hit
    return None


def _reason(e: BaseException) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code} from {e.request.method} {e.request.url.path}"
    return f"{type(e).__name__}: {e}"


@dataclass
class PinResult:
    definition_id: Any
    config_hash: str
    model_source: str                 # workflow_override | org_config | pasted | unknown
    model_names: dict
    tool_count: int | None            # hashed tool rows; None = tools not hashed
    tool_refs: int
    scope: list
    disclosures: list


def tool_url_template(harness_base: str, tool: str) -> str:
    """URL for a Dograh org-level http_api tool; Dograh renders {{call_id}} = callSid."""
    return f"{harness_base.rstrip('/')}/tools/{{{{call_id}}}}/{tool}"


def ws_base(endpoint: str) -> str:
    if endpoint.startswith("https://"):
        return "wss://" + endpoint[len("https://"):]
    if endpoint.startswith("http://"):
        return "ws://" + endpoint[len("http://"):]
    return endpoint


@dataclass
class DograhRun:
    id: Any
    workflow_id: Any
    definition_id: Any
    initial_context: dict
    raw: dict


class DograhClient:
    """The REST subset the harness uses (auth: header X-API-Key; prefix /api/v1)."""

    def __init__(self, base_url: str, api_key: str, *,
                 transport: httpx.AsyncBaseTransport | None = None, timeout_s: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self.base_url, transport=transport,
                                       headers={API_KEY_HEADER: api_key}, timeout=timeout_s)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        r = await self._http.request(method, API + path, **kw)
        r.raise_for_status()
        return r.json()

    async def create_run(self, workflow_id: Any, name: str) -> DograhRun:
        body = await self._json("POST", f"/workflow/{workflow_id}/runs",
                                json={"mode": "twilio", "name": name})
        ctx = body.get("initial_context") or {}
        return DograhRun(body["id"], body.get("workflow_id", workflow_id),
                         body.get("definition_id"), ctx, body)

    async def get_run(self, workflow_id: Any, run_id: Any) -> dict:
        return await self._json("GET", f"/workflow/{workflow_id}/runs/{run_id}")

    async def fetch_workflow(self, workflow_id: Any) -> dict:
        return await self._json("GET", f"/workflow/fetch/{workflow_id}")

    async def list_versions(self, workflow_id: Any) -> list[dict]:
        body = await self._json("GET", f"/workflow/{workflow_id}/versions")
        if isinstance(body, dict):      # UNCONFIRMED per docs/providers.md: list vs {"versions": [...]}
            body = body.get("versions", [])
        return list(body)

    async def list_tools(self) -> list[dict]:
        """`GET /api/v1/tools/` -> list[ToolResponse] (all statuses when unfiltered)."""
        body = await self._json("GET", TOOLS_PATH)
        if not isinstance(body, list):
            raise ValueError(f"expected a list, got {type(body).__name__}")
        return body

    async def get_org_model_config(self) -> dict:
        """`GET /api/v1/user/configurations/user` -> masked effective org model config."""
        body = await self._json("GET", ORG_MODEL_CONFIG_PATH)
        if not isinstance(body, dict):
            raise ValueError(f"expected an object, got {type(body).__name__}")
        return body

    async def get_model_connections(self) -> Any:
        """`GET /api/v1/model-connections/model-configurations` (hosted, B23); raw JSON."""
        return await self._json("GET", MODEL_CONNECTIONS_PATH)

    async def v3_override_models(self, override: dict, notes: list[str]) -> dict:
        """mode, provider (resolved by connection uuid), model, voice, language; never keys."""
        mode = override.get("mode")
        sec = override.get(mode) if isinstance(mode, str) else None
        sec = sec if isinstance(sec, dict) else {}
        settings = sec.get("settings") if isinstance(sec.get("settings"), dict) else {}
        conn = sec.get("provider_connection_uuid")
        provider: str | None = None
        if not conn:
            notes.append(f"{V3_OVERRIDE_KEY} has no provider_connection_uuid: provider unresolved")
        else:
            try:
                provider = find_connection_provider(await self.get_model_connections(), conn)
                if provider is None:
                    raise LookupError(f"connection {conn} not in the response")
            except (httpx.HTTPError, ValueError, TypeError, LookupError) as e:
                provider = None
                notes.append(f"model provider not resolved for connection {conn}: {_reason(e)}")
        if mode != "realtime":
            notes.append(f"{V3_OVERRIDE_KEY} mode is {mode!r}, not 'realtime': the run uses "
                         "that configuration, not the B21/B23 realtime path")
        return {"mode": mode,
                "provider": provider if provider is not None else f"unresolved:{conn}",
                "model": settings.get("model"), "voice": settings.get("voice"),
                "language": settings.get("language")}

    async def pin(self, workflow_id: Any, pasted_org_model_config: dict | None = None) -> PinResult:
        """Pin the current definition; hash it with the referenced tools and the model source."""
        wf = await self.fetch_workflow(workflow_id)
        def_id = wf.get("current_definition_id")
        versions = await self.list_versions(workflow_id)
        match = [v for v in versions if v.get("id") == def_id]
        if def_id is None or not match:
            raise DograhSetupError(f"workflow {workflow_id}: current definition {def_id!r} "
                                   "not in /versions")
        version = match[0]
        notes: list[str] = []
        scope = list(HASHED_FIELDS)

        # fix-35: org-level tool rows referenced by the pinned graph
        refs = referenced_tool_uuids(version.get("workflow_json"))
        tools: dict | None = None
        if refs:
            try:
                tools = tool_snapshot(await self.list_tools(), refs)
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as e:
                notes.append(f"tool definitions not hashed: {_reason(e)}")
            else:
                scope.append("tool_definitions (GET /api/v1/tools/)")
                if tools["missing"]:
                    notes.append(f"{tools['missing']} of {len(refs)} tool_uuids referenced by "
                                 "the pinned definition are not in GET /api/v1/tools/; the "
                                 "count enters the hash")

        # fix-36: where the model configuration comes from
        wcfg = version.get("workflow_configurations") or {}
        override = wcfg.get(OVERRIDE_KEY) if isinstance(wcfg, dict) else None
        v3 = wcfg.get(V3_OVERRIDE_KEY) if isinstance(wcfg, dict) else None
        org_cfg: dict | None = None
        names: dict = {}
        if v3 and isinstance(v3, dict):
            source = "workflow_override"
            names = await self.v3_override_models(v3, notes)
            if override:
                notes.append(f"both {V3_OVERRIDE_KEY} and {OVERRIDE_KEY} are set; "
                             f"{V3_OVERRIDE_KEY} (model config v3) is recorded")
        elif override:
            source, names = "workflow_override", override_models(override)
            if (names.get("mode"), names.get("byok_mode")) != ("byok", "realtime"):
                notes.append(f"{OVERRIDE_KEY} is not byok/realtime (mode={names.get('mode')!r}, "
                             f"byok.mode={names.get('byok_mode')!r}): the run uses that "
                             "configuration, not the B21 realtime path")
        else:
            try:
                org_cfg = org_config_snapshot(await self.get_org_model_config())
            except (httpx.HTTPError, ValueError, TypeError, AttributeError) as e:
                if pasted_org_model_config is not None:
                    source, org_cfg = "pasted", pasted_org_model_config
                    scope.append("org_model_config (pasted in the system YAML)")
                    notes.append(f"org model configuration not fetched ({_reason(e)}); the "
                                 "value pasted in params.org_model_config was hashed instead")
                else:
                    source = "unknown"
                    notes.append(f"org model configuration not hashed: {_reason(e)}; the "
                                 f"pinned definition has no {OVERRIDE_KEY}, so the models "
                                 "under test are not recorded")
            else:
                source = "org_config"
                scope.append("org_model_config (GET /api/v1/user/configurations/user)")
                for sec in ("llm", "stt", "tts", "realtime", "embeddings"):
                    svc = org_cfg.get(sec)
                    if isinstance(svc, dict):
                        names[sec] = {"provider": svc.get("provider"), "model": svc.get("model")}
                if pasted_org_model_config is not None:
                    notes.append("params.org_model_config ignored: the org model configuration "
                                 "was fetched")
        return PinResult(def_id, definition_hash(version, org_cfg, tool_definitions=tools),
                         source, names, None if tools is None else len(tools["rows"]),
                         len(refs), scope, notes)

    async def pin_definition(self, workflow_id: Any,
                             org_model_config: dict | None = None) -> tuple[Any, str]:
        """-> (definition_id, canonical hash) of the workflow's current definition."""
        r = await self.pin(workflow_id, org_model_config)
        return r.definition_id, r.config_hash

    async def create_http_tool(self, name: str, url: str, *, description: str = "",
                               method: str = "POST", parameters: list | None = None,
                               timeout_ms: int | None = None) -> dict:
        """Set-up helper: an org-level http_api tool (dograh-ingress.md (4))."""
        config: dict[str, Any] = {"method": method, "url": url, "parameters": parameters or [],
                                  "preset_parameters": []}
        if timeout_ms is not None:
            config["timeout_ms"] = timeout_ms
        return await self._json("POST", "/tools/", json={
            "name": name, "description": description,
            "category": "http_api",     # UNCONFIRMED per docs/providers.md: category value
            "definition": {"type": "http_api", "config": config}})


class DograhStack(TwilioAgentAdapter):
    name = "dograh_stack"
    needs_http_tools = True     # step 4: Dograh POSTs to {harness_tool_base}/tools/... (fix-44)

    def __init__(self, clock: "Clock", config: dict, *, connector: Connector | None = None,
                 tool_events: ToolEvents | None = None, client: DograhClient | None = None,
                 api_key: str | None = None, ws_token_secret: str | None = None):
        super().__init__(clock, config, connector=connector, tool_events=tool_events)
        a = self.acfg
        for k in REQUIRED_PARAMS:
            if a.get(k) in (None, ""):
                raise ValueError(f"dograh_stack: params.{k} is required")
        if client is None:
            env = a.get("api_key_env") or DEFAULT_API_KEY_ENV
            key = api_key if api_key is not None else os.environ.get(env, "")
            if not key and connector is None:   # an injected connector = test double, no key needed
                raise DograhSetupError(f"no Dograh API key (env {env})")
            client = DograhClient(a["endpoint"], key)
        self.client = client
        if ws_token_secret is None and a.get("ws_token_secret_env"):
            ws_token_secret = os.environ.get(a["ws_token_secret_env"]) or None
        self.ws_token_secret = ws_token_secret
        self.definition_id: Any = None        # pinned
        self.config_hash: str | None = None
        self.pinned: PinResult | None = None
        self.run: DograhRun | None = None

    @classmethod
    def preflight(cls, config: Any) -> list[str]:
        """Every precondition `__init__`/`open` would fail on, checked at plan time.

        No network: only `params` and the environment are read."""
        a = params_of(config)
        out: list[str] = []
        for k in REQUIRED_PARAMS:
            v = a.get(k)
            if v in (None, ""):
                out.append(f"dograh_stack: params.{k} is missing")
            elif v == PLACEHOLDER:
                out.append(f"dograh_stack: params.{k} is still the placeholder {PLACEHOLDER!r}")
        env = a.get("api_key_env") or DEFAULT_API_KEY_ENV
        if not os.environ.get(env):
            out.append(f"dograh_stack: Dograh API key env var {env} is unset")
        sec = a.get("ws_token_secret_env")
        if sec and not os.environ.get(sec):
            out.append(f"dograh_stack: params.ws_token_secret_env names {sec}, which is unset")
        return out

    async def pin(self) -> tuple[Any, str]:
        self.pinned = await self.client.pin(self.acfg["workflow_id"],
                                            self.acfg.get("org_model_config"))
        self.definition_id, self.config_hash = self.pinned.definition_id, self.pinned.config_hash
        return self.definition_id, self.config_hash

    async def _ws_url(self, call: "CallContext") -> str:
        a = self.acfg
        if self.config_hash is None:
            await self.pin()
        prefix = a.get("run_name_prefix", "DB-")
        run = await self.client.create_run(a["workflow_id"], f"{prefix}{call.tool_token}")
        if run.initial_context.get("provider") != "twilio":
            raise DograhSetupError(
                "run initial_context.provider != 'twilio': set the workflow's "
                "template_context_variables (dograh-ingress.md (1), step 2)")
        self.run = run
        base = ws_base(str(a.get("ws_endpoint") or a["endpoint"])).rstrip("/")
        url = f"{base}{API}/telephony/ws/{a['workflow_id']}/{a['org_id']}/{run.id}"
        if self.ws_token_secret:
            url += "/" + mint_token(self.ws_token_secret, a["workflow_id"], a["org_id"], run.id)
        return url

    def run_card_fields(self) -> dict:
        run_def = self.run.definition_id if self.run else None
        return {"workflow_id": self.acfg["workflow_id"], "org_id": self.acfg["org_id"],
                "definition_id": self.definition_id, "run_definition_id": run_def,
                "definition_matches_pinned": (None if self.run is None
                                              else run_def == self.definition_id),
                "config_hash": self.config_hash,
                "config_hash_scope": (list(self.pinned.scope) if self.pinned
                                      else list(HASHED_FIELDS)),
                "model_source": self.pinned.model_source if self.pinned else None,
                "model_names": dict(self.pinned.model_names) if self.pinned else {},
                "tool_definitions_hashed": self.pinned.tool_count if self.pinned else None,
                "tool_uuids_referenced": self.pinned.tool_refs if self.pinned else None,
                "ws_token": bool(self.ws_token_secret),
                "disclosures": list(DISCLOSURES) + (list(self.pinned.disclosures)
                                                    if self.pinned else [])}

    async def close(self) -> None:
        await super().close()
