"""Dograh stack adapter: harness as the Twilio carrier (BUILD_PLAN M6-T02, decision D2).

Every step comes from docs/dograh-ingress.md (static reading of Dograh at cb7f614;
FALLBACK B not required, so `components_only=False`):

1. `pin_definition(wf)`: `GET /api/v1/workflow/fetch/{wf}` -> `current_definition_id`,
   `GET /api/v1/workflow/{wf}/versions` -> that version's `workflow_json`,
   `workflow_configurations`, `template_context_variables`. Dograh keeps no live
   config hash (`workflow_hash` is legacy), so the harness hashes the pinned
   definition (+ the org model configuration, given in the system YAML because no
   endpoint for it was identified).
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
   (`tool_events` hook = ToolServer.subscribe).
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
DISCLOSURES = (
    "Runs are created with POST /api/v1/workflow/{wf}/runs (mode=twilio): no concurrency slot "
    "is taken and no quota check runs, unlike a production call (dograh-ingress.md (1)).",
    "POST /runs uses the draft definition when one exists; definition_id is recorded per call "
    "and compared with the pinned definition.",
    "At call end Dograh's Twilio hang-up strategy calls api.twilio.com with placeholder "
    "credentials and fails (logged side effect, not a billed call).",
    "Dograh sends no Twilio `mark`; playout timing comes from received media frames.",
)


class DograhSetupError(RuntimeError):
    """The Dograh workflow or org is not set up as dograh-ingress.md's script requires."""


def mint_token(secret: str, workflow_id: Any, org_id: Any, run_id: Any) -> str:
    """Dograh `ws_auth.mint_ws_token`: hex HMAC-SHA256 over "{wf}:{org}:{run}"."""
    msg = f"{workflow_id}:{org_id}:{run_id}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def definition_hash(version: dict, org_model_config: dict | None = None) -> str:
    """sha256 of the canonical JSON of the pinned definition's behavioural snapshot."""
    snap = {k: version.get(k) for k in HASHED_FIELDS}
    snap["org_model_config"] = org_model_config
    return hashlib.sha256(canonical_json(snap).encode()).hexdigest()


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

    async def pin_definition(self, workflow_id: Any,
                             org_model_config: dict | None = None) -> tuple[Any, str]:
        """-> (definition_id, canonical hash) of the workflow's current definition."""
        wf = await self.fetch_workflow(workflow_id)
        def_id = wf.get("current_definition_id")
        versions = await self.list_versions(workflow_id)
        match = [v for v in versions if v.get("id") == def_id]
        if def_id is None or not match:
            raise DograhSetupError(f"workflow {workflow_id}: current definition {def_id!r} "
                                   "not in /versions")
        return def_id, definition_hash(match[0], org_model_config)

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
        self.definition_id, self.config_hash = await self.client.pin_definition(
            self.acfg["workflow_id"], self.acfg.get("org_model_config"))
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
                "config_hash_scope": list(HASHED_FIELDS) + ["org_model_config (from YAML)"],
                "ws_token": bool(self.ws_token_secret),
                "disclosures": list(DISCLOSURES)}

    async def close(self) -> None:
        await super().close()
