"""Caller LLM: the simulated caller's "thinks" step (BUILD_PLAN M4-T08; spec §7).

    llm = CallerLLM(client, prompt_path=DEFAULT_PROMPT, config=None)
    text_or_end = await llm.next_utterance(ctx)       # str, or END when the caller hangs up

`client` is injected: in a run, `cli_run.build_sim_llm` builds an
`openai_compat.OpenAICompatClient` from the run config's `simulator.llm`; any client
exposing Anthropic-shaped `messages.create` works; tests use fakes (no live call is
made anywhere in the test suite). The request is one
`messages.create` with the rendered prompt as `system` and the call state as
the single `user` message, so a reply depends only on (prompt, ctx, config).

The agent side of `ctx.transcript` must be the reference-ASR text of the ear
recording, never the provider's own transcript (spec §7 "Hears").

Prompts are versioned files under `simulator/prompts/`; `prompt_hash` is the
sha256 of the file with line endings normalized to LF (so a Windows checkout
hashes the same as Linux), and `config_hash` covers prompt hash + config.

Model and sampling (see docs/decisions.md B8):
  * the live model comes from the run config's `simulator.llm` (decision B8: Pipecat's
    PhoneLLM Alpha 1 on an OpenAI-compatible endpoint, `cli_run.build_sim_llm`), which
    sets `model`, `temperature`, `send_temperature` and `max_tokens` here. An
    OpenAI-compatible endpoint accepts temperature 0, so spec §7's temperature 0 is
    sent there (`send_temperature: true`). DEFAULT_CONFIG's "claude-sonnet-5-5" stays
    as the default only because fakes and stored hashes depend on it.
  * For an Anthropic client: spec §7 says temperature 0. Claude Sonnet 5.5 rejects non-default sampling
    values with HTTP 400, and API version 2026-10-21 rejects `temperature` on
    every model, so `temperature` is kept in the config for the record but is
    sent only when `send_temperature` is True. Determinism therefore cannot come
    from sampling; it comes from the fixed prompt hash and the validation gate.
  * Thinking is left at the model default (adaptive); `effort` is sent only when
    set. Thinking tokens count toward `max_tokens`, hence the generous default.
"""
from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any, Iterable

from duplexbench.storage import canonical_json

END = "<END>"
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
DEFAULT_PROMPT = PROMPTS_DIR / "caller_v1.md"
DEFAULT_CONFIG: dict[str, Any] = {
    "model": "claude-sonnet-5-5",    # TO CONFIRM (spec §7; configs/simulator.yaml)
    "temperature": 0,                # spec §7; not sent by default, see module docstring
    "send_temperature": False,
    "effort": None,                  # output_config.effort when set; M4-T13 decides
    "max_tokens": 16000,             # covers adaptive-thinking tokens + a one-line reply
}


class CallerRefusal(RuntimeError):
    """The model declined the request (stop_reason == "refusal")."""


@dataclass
class SimContext:
    persona: str | None
    goal: str | None
    facts: dict
    steps: list[dict]                       # [{"id", "text", "self_correction": bool}]
    goal_entities: list[str]
    transcript: list[tuple[str, str]] = field(default_factory=list)   # ("agent"|"caller", text)
    tool_pending: bool = False

    @property
    def caller_turns(self) -> int:
        return sum(1 for who, _ in self.transcript if who == "caller")

    @classmethod
    def from_scenario(cls, scenario: Any, transcript: Iterable[tuple[str, str]] = (),
                      tool_pending: bool = False) -> "SimContext":
        """Build from a `scenarios.schema.Scenario`. Goal entities = each entity's
        `value` (or `text`) field; a step is a self-correction when its id or text
        says so (the scenario author marks it)."""
        ents = [str(e.get("value", e.get("text", ""))) for e in scenario.entities]
        steps = [{"id": s.id, "text": s.text,
                  "self_correction": "correct" in s.id.lower()} for s in scenario.steps]
        return cls(scenario.persona, scenario.goal, dict(scenario.facts), steps,
                   [e for e in ents if e], list(transcript), tool_pending)


def _norm_bytes(p: Path) -> bytes:
    return p.read_bytes().replace(b"\r\n", b"\n")


def prompt_hash(path: Path | str = DEFAULT_PROMPT) -> str:
    return hashlib.sha256(_norm_bytes(Path(path))).hexdigest()


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


class CallerLLM:
    def __init__(self, client: Any, prompt_path: Path | str = DEFAULT_PROMPT,
                 config: dict | None = None) -> None:
        self.client = client
        self.prompt_path = Path(prompt_path)
        self.template = Template(_norm_bytes(self.prompt_path).decode("utf-8"))
        self.prompt_hash = prompt_hash(self.prompt_path)
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self.config_hash = hashlib.sha256(canonical_json(
            {"prompt_hash": self.prompt_hash, "config": self.config}).encode()).hexdigest()
        self.calls = 0
        self.usage_log: list[dict] = []     # one {input_tokens, output_tokens} per response (fix-28)
        self._pending_end = False

    # -- rendering ---------------------------------------------------------------
    def system_prompt(self, ctx: SimContext) -> str:
        facts = "\n".join(f"- {k}: {v}" for k, v in sorted(ctx.facts.items())) or "- (none)"
        ents = "\n".join(f"- {e}" for e in ctx.goal_entities) or "- (none)"
        steps = "\n".join(
            f"{i + 1}. [{s['id']}]{' (self-correction)' if s.get('self_correction') else ''} "
            f"{s['text']}" for i, s in enumerate(ctx.steps)) or "(no script)"
        return self.template.substitute(persona=ctx.persona or "an ordinary caller",
                                        goal=ctx.goal or "(none given)", facts=facts,
                                        goal_entities=ents, steps=steps, end=END)

    def user_message(self, ctx: SimContext) -> str:
        lines = [f"{'AGENT' if who == 'agent' else 'YOU'}: {text}"
                 for who, text in ctx.transcript]
        pending = ("yes - the agent is looking something up; wait, or acknowledge briefly"
                   if ctx.tool_pending else "no")
        return ("Call so far (AGENT lines are what you heard on the phone line and may "
                "contain mishearings):\n" + ("\n".join(lines) or "(the call just connected)")
                + f"\n\nTool pending: {pending}\nYour turn number: {ctx.caller_turns + 1}"
                + "\nYour next line:")

    def request(self, ctx: SimContext) -> dict:
        cfg = self.config
        req: dict[str, Any] = {"model": cfg["model"], "max_tokens": cfg["max_tokens"],
                               "system": self.system_prompt(ctx),
                               "messages": [{"role": "user", "content": self.user_message(ctx)}]}
        if cfg.get("send_temperature"):
            req["temperature"] = cfg["temperature"]
        if cfg.get("effort"):
            req["output_config"] = {"effort": cfg["effort"]}
        return req

    # -- the call ----------------------------------------------------------------
    async def next_utterance(self, ctx: SimContext) -> str:
        """The caller's next line, or END. A reply that ends with END after some
        words returns the words now; the next call then returns END."""
        if self._pending_end:
            self._pending_end = False
            return END
        resp = self.client.messages.create(**self.request(ctx))
        if inspect.isawaitable(resp):
            resp = await resp                 # network wait: not wrapped in clock.idle
        self.calls += 1
        usage = _get(resp, "usage")
        if usage:                             # cli_run prices it when simulator.llm.price_ref is set
            self.usage_log.append({"input_tokens": int(_get(usage, "input_tokens", 0) or 0),
                                   "output_tokens": int(_get(usage, "output_tokens", 0) or 0)})
        if _get(resp, "stop_reason") == "refusal":
            raise CallerRefusal(str(_get(resp, "stop_details")))
        text = " ".join(_get(b, "text", "") for b in _get(resp, "content", [])
                        if _get(b, "type") == "text").strip()
        if END in text:
            before = text.split(END)[0].strip()
            if not before:
                return END
            self._pending_end = True
            return before
        return text
