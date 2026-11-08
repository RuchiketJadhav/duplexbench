"""OpenAI-compatible chat-completions client for the simulated caller (sim-llm; decision B8).

    client = OpenAICompatClient("http://10.0.0.5:8000/v1", model="pipecat-ai/phonellm-alpha-1",
                                api_key=None,
                                extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    llm = CallerLLM(client, config={"model": "pipecat-ai/phonellm-alpha-1",
                                    "temperature": 0, "send_temperature": True,
                                    "max_tokens": 400})
    text_or_end = await llm.next_utterance(ctx)

A drop-in for the Anthropic client `CallerLLM` expects: `client.messages.create(**req)`
(awaitable) takes the Anthropic-shaped request and returns an Anthropic-shaped response.
vLLM, SGLang and a Modal AutoEndpoint all serve `POST {base_url}/chat/completions`.

Request mapping (Anthropic req -> chat-completions JSON body):
  * `system`          -> first message {"role": "system", "content": system}
  * `messages`        -> copied after it ({"role": "user", "content": ...})
  * `model`           -> `model` (req's value, else the client's `model`)
  * `max_tokens`      -> `max_tokens`
  * `temperature`     -> `temperature`, only when present in req (CallerLLM's send_temperature)
  * `output_config`   -> dropped (Anthropic-only effort control; no OpenAI equivalent)
  * `extra_body`      -> dict-merged into the body last, so `chat_template_kwargs` lands at
                         the top level, where vLLM / SGLang read it
  * header `Authorization: Bearer <api_key>` only when api_key is given

Response mapping (choices[0] -> Anthropic-shaped):
  * `content`      = [{"type": "text", "text": message.content}]
  * `stop_reason`  = finish_reason stop -> "end_turn", length -> "max_tokens",
                     content_filter -> "refusal", anything else -> the raw string
  * `stop_details` = message.refusal text when given, else the raw finish_reason
  * `usage`        = {"input_tokens": prompt_tokens, "output_tokens": completion_tokens} (0 if absent)
  * `model`        = the response's `model`, else the request's

Errors: a non-2xx status, a connection error, a timeout or a response without
`choices[0].message.content` raise SimLLMError (status code + first 300 chars of the body).

PhoneLLM Alpha 1 (`pipecat-ai/phonellm-alpha-1`, a fine-tune of NVIDIA Nemotron 3 Nano
30B-A3B) recommended settings, from the model card
(https://huggingface.co/pipecat-ai/phonellm-alpha-1, as recorded in docs/decisions.md B8;
not re-fetched by this module's tests): `temperature=0` and thinking
disabled with `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`.
The endpoint is self-hosted GPU time billed outside the harness (cli_run records
`billing: external`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

BODY_CHARS = 300   # threshold-ok: error-message body excerpt length, not a spec threshold
STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}


class SimLLMError(RuntimeError):
    """The OpenAI-compatible endpoint failed or returned no assistant text."""


@dataclass
class ChatResponse:
    content: list[dict]
    stop_reason: str | None
    stop_details: str | None
    usage: dict = field(default_factory=dict)
    model: str | None = None


def normalize_base_url(base_url: str) -> str:
    """Trailing slashes stripped; a `/v1` suffix is kept as given."""
    return base_url.strip().rstrip("/")


class _Messages:
    def __init__(self, owner: "OpenAICompatClient") -> None:
        self._owner = owner

    async def create(self, **req: Any) -> ChatResponse:
        return await self._owner._create(req)


class OpenAICompatClient:
    def __init__(self, base_url: str, model: str | None = None, api_key: str | None = None,
                 extra_body: dict | None = None, timeout_s: float = 60.0,
                 transport: Any = None) -> None:
        self.base_url = normalize_base_url(base_url)
        self.model = model
        self.api_key = api_key
        self.extra_body = dict(extra_body or {})
        self.timeout_s = float(timeout_s)
        self.transport = transport              # httpx.MockTransport in tests
        self.messages = _Messages(self)

    @property
    def url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def body(self, req: dict) -> dict:
        msgs: list[dict] = []
        if req.get("system"):
            msgs.append({"role": "system", "content": req["system"]})
        msgs.extend(dict(m) for m in req.get("messages") or [])
        out: dict[str, Any] = {"model": req.get("model") or self.model, "messages": msgs}
        if "max_tokens" in req:
            out["max_tokens"] = req["max_tokens"]
        if "temperature" in req:
            out["temperature"] = req["temperature"]
        out.update(self.extra_body)             # last: chat_template_kwargs at top level
        return out

    def headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def _create(self, req: dict) -> ChatResponse:
        body = self.body(req)
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s,
                                         transport=self.transport) as http:
                r = await http.post(self.url, json=body, headers=self.headers())
        except httpx.TimeoutException as e:
            raise SimLLMError(f"timeout after {self.timeout_s} s: POST {self.url}: {e!r}") from e
        except httpx.HTTPError as e:
            raise SimLLMError(f"connection error: POST {self.url}: {e!r}") from e
        text = r.text
        if not r.is_success:
            raise SimLLMError(f"HTTP {r.status_code} from {self.url}: {text[:BODY_CHARS]}")
        try:
            data = r.json()
            choice = data["choices"][0]
            msg = choice["message"]
            content = msg["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise SimLLMError(f"HTTP {r.status_code} from {self.url}: no "
                              f"choices[0].message.content: {text[:BODY_CHARS]}") from e
        finish = choice.get("finish_reason")
        refusal = msg.get("refusal")
        if content is None and not refusal:
            raise SimLLMError(f"HTTP {r.status_code} from {self.url}: no "
                              f"choices[0].message.content: {text[:BODY_CHARS]}")
        stop = "refusal" if refusal else STOP_REASONS.get(finish, finish)
        usage = data.get("usage") or {}
        return ChatResponse(
            content=[{"type": "text", "text": content or ""}],
            stop_reason=stop,
            stop_details=str(refusal) if refusal else finish,
            usage={"input_tokens": int(usage.get("prompt_tokens") or 0),
                   "output_tokens": int(usage.get("completion_tokens") or 0)},
            model=data.get("model") or body.get("model"))
