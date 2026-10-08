"""Tool calling in the interviewer: the allowlist, validation, caps and audit.

Three layers, tested separately:

* the tools themselves -- what a malformed or hostile call turns into;
* the real provider's stream parser -- tool calls arrive as JSON fragments;
* the interview route -- rounds, caps, the SSE ``tool`` event and the audit
  rows, run through the API against real PostgreSQL.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from sqlalchemy import func, select

from app.llm.client import API_URL, AnthropicProvider, StreamResult, ToolCallRequest, Usage
from app.llm.prompts import scorer_system
from app.llm.tools import MAX_CALLS_PER_ROUND, MAX_TOOL_ROUNDS, ToolContext, run_tool, tool_specs

pytestmark = pytest.mark.asyncio

NEW_SESSION = {
    "role_title": "Fullstack Developer (Python/React)",
    "focus_areas": ["FastAPI", "React", "PostgreSQL"],
    "seniority": "senior",
}

CTX = ToolContext(
    focus_areas=["FastAPI", "React", "PostgreSQL"],
    transcript=["Tell me about a FastAPI service you run.", "It sits on postgresql 16."],
    questions_asked=1,
    max_questions=6,
)


# --- The tools ---------------------------------------------------------------


async def test_an_unknown_tool_becomes_an_error_result_not_an_exception():
    outcome = await run_tool("delete_session", {}, CTX)
    assert outcome.ok is False
    assert "unknown tool" in outcome.result["error"]


async def test_inputs_are_validated_before_any_handler_runs():
    outcome = await run_tool("flag_follow_up", {"topic": "scale", "reason": "because"}, CTX)
    assert outcome.ok is False
    assert outcome.result["error"] == "invalid input"
    assert outcome.result["problems"][0]["field"] == "reason"


async def test_no_tool_accepts_a_session_id():
    """The session is bound on the server. A model talked into looking at
    another interview has no field to put the id in -- and an extra field is an
    error, not an ignored key."""
    for name in ("get_coverage", "get_rubric", "flag_follow_up"):
        outcome = await run_tool(name, {"session_id": "00000000-0000-0000-0000-000000000000"}, CTX)
        assert outcome.ok is False, name
    assert "session_id" not in json.dumps(tool_specs())


async def test_coverage_counts_what_is_left_and_matches_focus_areas_case_insensitively():
    outcome = await run_tool("get_coverage", {}, CTX)
    assert outcome.ok is True
    assert outcome.result["questions_remaining"] == 5
    mentioned = {a["area"]: a["mentioned"] for a in outcome.result["focus_areas"]}
    assert mentioned == {"FastAPI": True, "React": False, "PostgreSQL": True}


async def test_the_rubric_has_exactly_the_dimensions_the_scorer_scores():
    """If the scorer gains a dimension the interviewer can't look up, the
    interview stops probing what gets scored. This holds the two together."""
    (rubric,) = [s for s in tool_specs() if s["name"] == "get_rubric"]
    dimensions = rubric["input_schema"]["properties"]["dimension"]["enum"]
    assert len(dimensions) == 5
    for dimension in dimensions:
        assert f'"{dimension}"' in scorer_system()
        outcome = await run_tool("get_rubric", {"dimension": dimension}, CTX)
        assert set(outcome.result["levels"]) == {"1", "3", "5"}


# --- The real provider's stream parser --------------------------------------


class _Lines:
    status_code = 200

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.request = httpx.Request("POST", API_URL)

    def raise_for_status(self) -> None:
        return None

    async def aread(self) -> bytes:
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamingClient:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.payloads: list[dict] = []

    @asynccontextmanager
    async def stream(self, _method, _url, json=None):
        self.payloads.append(json)
        yield _Lines(self._lines)


def _provider(client: _StreamingClient) -> AnthropicProvider:
    provider = object.__new__(AnthropicProvider)
    provider._max_attempts = 1
    provider._client = client
    provider._backoff = lambda _attempt: 0
    return provider


def _data(event: dict) -> str:
    return "data: " + json.dumps(event)


def _tool_stream(*fragments: str) -> list[str]:
    return [
        _data({"type": "message_start", "message": {"usage": {"input_tokens": 40}}}),
        _data(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "flag_follow_up", "input": {}},
            }
        ),
        *[
            _data({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": f}})
            for f in fragments
        ],
        _data({"type": "content_block_stop", "index": 0}),
        _data({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 12}}),
        _data({"type": "message_stop"}),
    ]


async def test_the_real_provider_assembles_a_tool_call_from_json_fragments():
    client = _StreamingClient(_tool_stream('{"topic": "the cache', '", "reason": "no_number"}'))
    result = StreamResult()

    pieces = [
        p
        async for p in _provider(client).stream(
            system="s", messages=[], model="m", result=result, tools=tool_specs()
        )
    ]

    assert pieces == [], "a tool call is not text for the candidate"
    assert result.stop_reason == "tool_use"
    assert [(c.id, c.name, c.input) for c in result.tool_calls] == [
        ("toolu_1", "flag_follow_up", {"topic": "the cache", "reason": "no_number"})
    ]
    assert [t["name"] for t in client.payloads[0]["tools"]] == [
        "get_coverage",
        "get_rubric",
        "flag_follow_up",
    ]
    assert (result.usage.input_tokens, result.usage.output_tokens) == (40, 12)


async def test_tools_are_sent_only_when_offered():
    client = _StreamingClient(
        [
            _data({"type": "message_start", "message": {"usage": {"input_tokens": 3}}}),
            _data({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Why?"}}),
            _data({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
        ]
    )
    result = StreamResult()

    pieces = [p async for p in _provider(client).stream(system="s", messages=[], model="m", result=result)]

    assert pieces == ["Why?"]
    assert "tools" not in client.payloads[0]
    assert result.tool_calls == []


async def test_a_call_whose_json_never_closed_reaches_the_model_as_an_error():
    client = _StreamingClient(_tool_stream('{"topic": "the cache'))
    result = StreamResult()
    _ = [p async for p in _provider(client).stream(system="s", messages=[], model="m", result=result, tools=tool_specs())]

    (call,) = result.tool_calls
    assert call.input is None
    outcome = await run_tool(call.name, call.input, CTX)
    assert outcome.ok is False
    assert "JSON object" in outcome.result["error"]


# --- Through the interview route ---------------------------------------------


async def _create(client) -> str:
    resp = await client.post("/sessions", json=NEW_SESSION)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _events(client, session_id: str) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    name = None
    async with client.stream("GET", f"/sessions/{session_id}/stream") as resp:
        assert resp.status_code == 200, await resp.aread()
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:") and name:
                out.append((name, json.loads(line[5:].strip())))
    return out


async def _audit(client, session_id: str) -> list[dict]:
    resp = await client.get(f"/sessions/{session_id}/tool-calls")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_the_first_question_checks_coverage_before_it_is_asked(auth_client):
    session_id = await _create(auth_client)

    events = await _events(auth_client, session_id)

    names = [name for name, _ in events]
    assert names[:2] == ["start", "tool"]
    assert events[1][1] == {"name": "get_coverage"}
    assert names[-1] == "done"
    (call,) = await _audit(auth_client, session_id)
    assert (call["name"], call["ok"], call["turn_index"], call["round"]) == ("get_coverage", True, 0, 0)
    assert call["result"]["questions_remaining"] == 6


async def test_an_answer_with_no_number_is_flagged_and_asked_about(auth_client):
    session_id = await _create(auth_client)
    await _events(auth_client, session_id)
    await auth_client.post(
        f"/sessions/{session_id}/answers",
        json={"content": "We made checkout much faster after the migration."},
    )

    events = await _events(auth_client, session_id)

    flagged = [c for c in await _audit(auth_client, session_id) if c["name"] == "flag_follow_up"]
    assert len(flagged) == 1
    assert flagged[0]["arguments"]["reason"] == "no_number"
    assert flagged[0]["turn_index"] == 2
    assert "We made checkout much faster" in events[-1][1]["content"]


async def test_an_answer_with_numbers_is_not_flagged(auth_client):
    session_id = await _create(auth_client)
    await _events(auth_client, session_id)
    await auth_client.post(
        f"/sessions/{session_id}/answers",
        json={"content": "p95 went from 900 ms to 180 ms for 1.9M users."},
    )

    await _events(auth_client, session_id)

    names = [c["name"] for c in await _audit(auth_client, session_id)]
    assert names == ["get_coverage"]


async def test_the_audit_is_readable_only_by_the_sessions_owner(auth_client, other_client):
    session_id = await _create(auth_client)
    await _events(auth_client, session_id)

    resp = await other_client.get(f"/sessions/{session_id}/tool-calls")

    assert resp.status_code == 404


async def test_a_model_that_keeps_calling_tools_still_has_to_ask(auth_client, app):
    class Greedy:
        name = "greedy-test"
        supports_tools = True

        def __init__(self) -> None:
            self.offered: list[bool] = []

        async def stream(self, *, system, messages, model, result, tools=None):
            self.offered.append(tools is not None)
            result.usage = Usage(model=model, input_tokens=1, output_tokens=1)
            if tools:
                result.tool_calls = [ToolCallRequest(id=f"t{len(self.offered)}", name="get_coverage", input={})]
                result.stop_reason = "tool_use"
                return
            result.text = "What would break first at ten times the traffic?"
            yield result.text

    greedy = Greedy()
    app.state.llm = greedy
    session_id = await _create(auth_client)

    events = await _events(auth_client, session_id)

    assert greedy.offered == [True] * MAX_TOOL_ROUNDS + [False]
    assert events[-1][0] == "done"
    assert len(await _audit(auth_client, session_id)) == MAX_TOOL_ROUNDS


async def test_calls_past_the_per_turn_cap_are_answered_with_an_error_and_not_run(auth_client, app):
    seen: list[list[dict]] = []

    class Burst:
        name = "burst-test"
        supports_tools = True

        async def stream(self, *, system, messages, model, result, tools=None):
            seen.append(messages)
            result.usage = Usage(model=model, input_tokens=1, output_tokens=1)
            if tools and not isinstance(messages[-1]["content"], list):
                result.tool_calls = [
                    ToolCallRequest(id=f"t{i}", name="get_rubric", input={"dimension": "Structure"})
                    for i in range(MAX_CALLS_PER_ROUND + 2)
                ]
                result.stop_reason = "tool_use"
                return
            result.text = "Walk me through the hardest constraint you shipped under."
            yield result.text

    app.state.llm = Burst()
    session_id = await _create(auth_client)

    events = await _events(auth_client, session_id)

    assert events[-1][0] == "done"
    assert [name for name, _ in events].count("tool") == MAX_CALLS_PER_ROUND
    audit = await _audit(auth_client, session_id)
    assert [c["ok"] for c in audit] == [True] * MAX_CALLS_PER_ROUND + [False, False]
    results = seen[1][-1]["content"]
    assert [r["tool_use_id"] for r in results] == [f"t{i}" for i in range(MAX_CALLS_PER_ROUND + 2)]
    assert [r["is_error"] for r in results] == [False] * MAX_CALLS_PER_ROUND + [True, True]


async def test_a_failing_call_does_not_end_the_interview(auth_client, app):
    class Confused:
        name = "confused-test"
        supports_tools = True

        async def stream(self, *, system, messages, model, result, tools=None):
            result.usage = Usage(model=model, input_tokens=1, output_tokens=1)
            if tools and not isinstance(messages[-1]["content"], list):
                result.tool_calls = [ToolCallRequest(id="t0", name="drop_table", input={})]
                result.stop_reason = "tool_use"
                return
            result.text = "Tell me about a bug that took far too long to find."
            yield result.text

    app.state.llm = Confused()
    session_id = await _create(auth_client)

    events = await _events(auth_client, session_id)

    assert events[-1][0] == "done"
    (call,) = await _audit(auth_client, session_id)
    assert (call["name"], call["ok"]) == ("drop_table", False)


async def test_deleting_a_session_deletes_its_tool_calls(auth_client, session_factory):
    from app.models import ToolCall

    session_id = await _create(auth_client)
    await _events(auth_client, session_id)
    assert len(await _audit(auth_client, session_id)) == 1

    assert (await auth_client.delete(f"/sessions/{session_id}")).status_code == 204

    async with session_factory() as s:
        left = await s.scalar(select(func.count()).select_from(ToolCall))
    assert left == 0
