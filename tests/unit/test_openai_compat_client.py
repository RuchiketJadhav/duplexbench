"""sim-llm: OpenAICompatClient request/response mapping over httpx.MockTransport (no network)."""
from __future__ import annotations

import json

import httpx
import pytest

from duplexbench.simulator.caller_llm import END, CallerLLM, CallerRefusal, SimContext
from duplexbench.simulator.openai_compat import OpenAICompatClient, SimLLMError

PHONELLM = "pipecat-ai/phonellm-alpha-1"
EXTRA = {"chat_template_kwargs": {"enable_thinking": False}}


def _ok(text="Hello there.", finish="stop", usage=True, **msg_extra):
    body = {"id": "x", "model": PHONELLM,
            "choices": [{"index": 0, "finish_reason": finish,
                         "message": {"role": "assistant", "content": text, **msg_extra}}]}
    if usage:
        body["usage"] = {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127}
    return body


class Recorder:
    def __init__(self, *responses):
        self.requests: list[httpx.Request] = []
        self.responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, httpx.Response):
            return r
        return httpx.Response(200, json=r)

    def body(self, i=-1) -> dict:
        return json.loads(self.requests[i].content)


def _client(rec, api_key=None, base="http://10.0.0.5:8000/v1/"):
    return OpenAICompatClient(base, model=PHONELLM, api_key=api_key, extra_body=EXTRA,
                              transport=httpx.MockTransport(rec))


REQ = {"model": PHONELLM, "max_tokens": 400, "system": "You are a caller.",
       "messages": [{"role": "user", "content": "Your next line:"}]}


async def test_request_body_and_url():
    rec = Recorder(_ok())
    await _client(rec).messages.create(**REQ, output_config={"effort": "low"})
    req = rec.requests[0]
    assert str(req.url) == "http://10.0.0.5:8000/v1/chat/completions"
    assert req.method == "POST" and "authorization" not in req.headers
    b = rec.body()
    assert b["messages"] == [{"role": "system", "content": "You are a caller."},
                             {"role": "user", "content": "Your next line:"}]
    assert b["max_tokens"] == 400 and b["model"] == PHONELLM
    assert "temperature" not in b and "output_config" not in b
    assert b["chat_template_kwargs"] == {"enable_thinking": False}     # top level


async def test_temperature_only_when_in_req_and_auth_header():
    rec = Recorder(_ok())
    await _client(rec, api_key="sk-test").messages.create(**REQ, temperature=0)
    assert rec.body()["temperature"] == 0
    assert rec.requests[0].headers["authorization"] == "Bearer sk-test"


def test_base_url_normalized():
    assert OpenAICompatClient("https://h.example/v1//").url == "https://h.example/v1/chat/completions"
    assert OpenAICompatClient("https://h.example").url == "https://h.example/chat/completions"


@pytest.mark.parametrize("finish,stop", [("stop", "end_turn"), ("length", "max_tokens"),
                                         ("content_filter", "refusal"),
                                         ("tool_calls", "tool_calls")])
async def test_stop_reason_mapping(finish, stop):
    r = await _client(Recorder(_ok(finish=finish))).messages.create(**REQ)
    assert r.stop_reason == stop and r.stop_details == finish
    assert r.content == [{"type": "text", "text": "Hello there."}]
    assert r.usage == {"input_tokens": 120, "output_tokens": 7} and r.model == PHONELLM


async def test_usage_absent_is_zero_and_refusal_text():
    r = await _client(Recorder(_ok(usage=False))).messages.create(**REQ)
    assert r.usage == {"input_tokens": 0, "output_tokens": 0}
    r2 = await _client(Recorder(_ok(text=None, refusal="I can't help"))).messages.create(**REQ)
    assert r2.stop_reason == "refusal" and r2.stop_details == "I can't help"


async def test_errors():
    with pytest.raises(SimLLMError, match="HTTP 500.*boom"):
        await _client(Recorder(httpx.Response(500, text="boom"))).messages.create(**REQ)
    with pytest.raises(SimLLMError, match="no choices"):
        await _client(Recorder({"object": "error"})).messages.create(**REQ)
    with pytest.raises(SimLLMError, match="no choices"):
        await _client(Recorder(httpx.Response(200, text="not json"))).messages.create(**REQ)
    long = "x" * 1000
    with pytest.raises(SimLLMError) as e:
        await _client(Recorder(httpx.Response(503, text=long))).messages.create(**REQ)
    assert str(e.value).count("x") <= 300 + 10

    def boom(request):
        raise httpx.ConnectError("refused", request=request)
    with pytest.raises(SimLLMError, match="connection error"):
        await _client(boom).messages.create(**REQ)

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)
    with pytest.raises(SimLLMError, match="timeout"):
        await _client(slow).messages.create(**REQ)


def _ctx():
    return SimContext(persona="a busy parent", goal="move the booking", facts={"booking": "BK4471"},
                      steps=[{"id": "s1", "text": "ask to move", "self_correction": False}],
                      goal_entities=["BK4471"], transcript=[("agent", "Hello, how can I help?")])


async def test_caller_llm_end_to_end():
    rec = Recorder(_ok("I need to move my booking."), _ok(f"Thanks, bye. {END}"))
    llm = CallerLLM(_client(rec), config={"model": PHONELLM, "temperature": 0,
                                         "send_temperature": True, "max_tokens": 400})
    assert await llm.next_utterance(_ctx()) == "I need to move my booking."
    b = rec.body(0)
    assert b["messages"][0]["role"] == "system" and "BK4471" in b["messages"][0]["content"]
    assert b["messages"][1]["role"] == "user" and b["temperature"] == 0
    assert b["max_tokens"] == 400 and b["chat_template_kwargs"] == {"enable_thinking": False}
    assert await llm.next_utterance(_ctx()) == "Thanks, bye."
    assert await llm.next_utterance(_ctx()) == END          # pending END, no request
    assert len(rec.requests) == 2 and llm.calls == 2


async def test_caller_llm_bare_end_and_refusal():
    llm = CallerLLM(_client(Recorder(_ok(END))), config={"model": PHONELLM})
    assert await llm.next_utterance(_ctx()) == END
    llm2 = CallerLLM(_client(Recorder(_ok(finish="content_filter"))), config={"model": PHONELLM})
    with pytest.raises(CallerRefusal):
        await llm2.next_utterance(_ctx())
