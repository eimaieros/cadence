"""Inputs the database cannot store are refused as client errors, not crashes.

PostgreSQL rejects U+0000 in TEXT and JSONB. Before this file, one NUL in a
request body reached the INSERT and came back as a 500 -- a malformed input
reported as the server's fault, with a stack trace in the logs, on demand.
Each case here sends the character through a different stored field and holds
that the answer is a 422 and that nothing was written.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.llm.scoring import ScoreDraft
from app.llm.tools import ToolContext, run_tool
from app.models import InterviewSession, JobDescriptionChunk, Turn, User

pytestmark = pytest.mark.asyncio

NUL = "\x00"
SESSION = {"role_title": "Backend Engineer", "focus_areas": ["PostgreSQL"], "seniority": "mid"}


async def _rows(session_factory, model) -> int:
    async with session_factory() as s:
        return await s.scalar(select(func.count()).select_from(model))


@pytest.mark.parametrize(
    "body",
    [
        {**SESSION, "role_title": f"Back{NUL}end"},
        {**SESSION, "focus_areas": [f"Post{NUL}greSQL"]},
        {**SESSION, "job_description": f"- PostgreSQL{NUL} migrations"},
    ],
    ids=["role title", "focus area", "job description"],
)
async def test_a_nul_in_a_new_session_is_a_422_and_stores_nothing(auth_client, session_factory, body):
    resp = await auth_client.post("/sessions", json=body)

    assert resp.status_code == 422
    assert await _rows(session_factory, InterviewSession) == 0
    assert await _rows(session_factory, JobDescriptionChunk) == 0


async def test_a_nul_in_an_answer_is_a_422_and_stores_nothing(auth_client, session_factory):
    session_id = (await auth_client.post("/sessions", json=SESSION)).json()["id"]

    resp = await auth_client.post(f"/sessions/{session_id}/answers", json={"content": f"an{NUL}swer"})

    assert resp.status_code == 422
    assert await _rows(session_factory, Turn) == 0


async def test_a_nul_in_a_display_name_is_a_422_and_creates_no_account(client, session_factory):
    resp = await client.post(
        "/auth/register",
        json={"email": "nul@example.com", "password": "a-long-enough-password", "display_name": f"Ro{NUL}drigo"},
    )

    assert resp.status_code == 422
    assert await _rows(session_factory, User) == 0


@pytest.mark.parametrize(
    "name,args",
    [
        ("flag_follow_up", {"topic": f"latency{NUL}", "reason": "no_number"}),
        ("search_job_description", {"query": f"postgres{NUL}"}),
    ],
)
async def test_a_nul_in_a_tool_call_is_an_invalid_input_result(name, args):
    async def search(query: str, k: int) -> list[dict]:
        raise AssertionError("an invalid call must not reach the search")

    ctx = ToolContext(
        focus_areas=[], transcript=[], questions_asked=1, max_questions=6, search_job_description=search
    )

    outcome = await run_tool(name, args, ctx)

    assert outcome.ok is False
    assert outcome.result["error"] == "invalid input"


async def test_a_nul_in_the_scorers_json_fails_validation_and_gets_the_retry():
    draft = {
        "overall": 70,
        "summary": f"Solid{NUL}",
        "dimensions": [
            {"name": n, "score": 3, "note": "ok"}
            for n in ("Technical depth", "Structure", "Specificity", "Trade-off reasoning", "Communication")
        ],
        "strengths": [],
        "gaps": [],
    }

    with pytest.raises(ValidationError):
        ScoreDraft.model_validate(draft)


# --- bcrypt off the event loop ------------------------------------------------


async def _ticks_during(coro) -> int:
    """How often the event loop got to run another task while ``coro`` ran."""
    ticks = 0
    done = asyncio.Event()

    async def ticker() -> None:
        nonlocal ticks
        while not done.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        await coro
    finally:
        done.set()
        await task
    return ticks


async def test_a_login_does_not_hold_the_event_loop(client, monkeypatch):
    import app.routers.auth as auth_module

    def slow_verify(plain: str, hashed: str) -> bool:
        time.sleep(0.3)  # stands in for bcrypt: CPU-bound and blocking
        return False

    monkeypatch.setattr(auth_module, "verify_password", slow_verify)

    ticks = await _ticks_during(
        client.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever-it-is"})
    )

    # On the loop, the ticker would get no turn for those 300 ms.
    assert ticks >= 10


async def test_a_registration_does_not_hold_the_event_loop(client, monkeypatch):
    import app.routers.auth as auth_module

    real = auth_module.hash_password

    def slow_hash(plain: str) -> str:
        time.sleep(0.3)
        return real(plain)

    monkeypatch.setattr(auth_module, "hash_password", slow_hash)

    ticks = await _ticks_during(
        client.post(
            "/auth/register",
            json={"email": "slow@example.com", "password": "a-long-enough-password", "display_name": "Slow"},
        )
    )

    assert ticks >= 10
