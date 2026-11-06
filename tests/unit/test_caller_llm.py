"""M4-T08: caller LLM with a fake client (no live API call anywhere)."""
import asyncio
from types import SimpleNamespace

import pytest

from duplexbench.scenarios.schema import Scenario
from duplexbench.simulator.caller_llm import (DEFAULT_CONFIG, DEFAULT_PROMPT, END, CallerLLM,
                                              CallerRefusal, SimContext, prompt_hash)


class FakeMessages:
    def __init__(self, replies, stop_reason="end_turn"):
        self.replies = list(replies)
        self.requests = []
        self.stop_reason = stop_reason

    async def create(self, **kw):
        self.requests.append(kw)
        text = self.replies.pop(0)
        return SimpleNamespace(stop_reason=self.stop_reason, stop_details=None,
                               content=[SimpleNamespace(type="thinking", thinking=""),
                                        SimpleNamespace(type="text", text=text)])


class FakeClient:
    def __init__(self, replies, **kw):
        self.messages = FakeMessages(replies, **kw)


def _ctx(pending=False):
    return SimContext(persona="busy parent", goal="book a dental cleaning",
                      facts={"name": "Ana Ruiz", "phone": "415 555 0100"},
                      steps=[{"id": "s1", "text": "I need a cleaning."},
                             {"id": "s2_correct", "text": "Tuesday, no sorry, Wednesday.",
                              "self_correction": True}],
                      goal_entities=["Ana Ruiz", "415 555 0100"],
                      transcript=[("agent", "Thanks for calling, how can I help?")],
                      tool_pending=pending)


def test_prompt_contains_goal_entities_and_pending_flag():
    client = FakeClient(["Hi, this is Ana Ruiz."])
    llm = CallerLLM(client)
    out = asyncio.run(llm.next_utterance(_ctx(pending=True)))
    assert out == "Hi, this is Ana Ruiz."
    req = client.messages.requests[0]
    assert "Ana Ruiz" in req["system"] and "415 555 0100" in req["system"]
    assert "(self-correction)" in req["system"]
    assert "Tool pending: yes" in req["messages"][0]["content"]
    assert "AGENT: Thanks for calling" in req["messages"][0]["content"]
    assert req["model"] == DEFAULT_CONFIG["model"] == "claude-sonnet-5-5"
    assert "temperature" not in req                       # Sonnet 5.5 rejects it; see module doc
    llm2 = CallerLLM(FakeClient(["x"]))
    asyncio.run(llm2.next_utterance(_ctx(pending=False)))
    assert "Tool pending: no" in llm2.client.messages.requests[0]["messages"][0]["content"]


def test_end_sentinel():
    assert asyncio.run(CallerLLM(FakeClient([END])).next_utterance(_ctx())) == END
    assert asyncio.run(CallerLLM(FakeClient([f" {END} "])).next_utterance(_ctx())) == END
    llm = CallerLLM(FakeClient([f"Great, thanks, bye. {END}"]))
    assert asyncio.run(llm.next_utterance(_ctx())) == "Great, thanks, bye."
    assert asyncio.run(llm.next_utterance(_ctx())) == END
    assert llm.calls == 1


def test_prompt_hash_stable_and_line_ending_independent(tmp_path):
    h = prompt_hash()
    assert h == prompt_hash(DEFAULT_PROMPT) == CallerLLM(FakeClient([])).prompt_hash
    crlf = tmp_path / "caller_v1.md"
    crlf.write_bytes(DEFAULT_PROMPT.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert prompt_hash(crlf) == h
    assert CallerLLM(FakeClient([]), config={"effort": "low"}).config_hash != \
        CallerLLM(FakeClient([])).config_hash


def test_optional_request_fields():
    llm = CallerLLM(FakeClient([]), config={"send_temperature": True, "effort": "low"})
    req = llm.request(_ctx())
    assert req["temperature"] == 0 and req["output_config"] == {"effort": "low"}


def test_refusal_raises():
    with pytest.raises(CallerRefusal):
        asyncio.run(CallerLLM(FakeClient(["no"], stop_reason="refusal")).next_utterance(_ctx()))


def test_from_scenario():
    sc = Scenario(id="sch-01", version="1", split="public-dev", domain="scheduling", track="C",
                  source="simulated", caller_voice="v1", persona="p", goal="g",
                  facts={"dob": "1990-03-03"},
                  steps=[{"id": "s1", "text": "hi"}, {"id": "s2-correction", "text": "no, 4"}],
                  entities=[{"type": "date", "value": "March 3 1990"}, {"type": "x"}])
    ctx = SimContext.from_scenario(sc, [("agent", "hello")], tool_pending=True)
    assert ctx.goal_entities == ["March 3 1990"]
    assert [s["self_correction"] for s in ctx.steps] == [False, True]
    assert ctx.caller_turns == 0 and ctx.tool_pending
