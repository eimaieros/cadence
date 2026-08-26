"""Interview session tests, including the streaming path and tenancy isolation."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio

NEW_SESSION = {
    "role_title": "Fullstack Developer (Python/React)",
    "focus_areas": ["FastAPI", "React", "PostgreSQL"],
    "seniority": "senior",
}


async def _create(client) -> str:
    resp = await client.post("/sessions", json=NEW_SESSION)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _consume_stream(client, session_id: str) -> list[str]:
    """Read an SSE response and return the event names in order."""
    events: list[str] = []
    async with client.stream("GET", f"/sessions/{session_id}/stream") as resp:
        assert resp.status_code == 200, await resp.aread()
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
    return events


# --- CRUD -----------------------------------------------------------------


async def test_create_and_fetch_session(auth_client):
    session_id = await _create(auth_client)
    resp = await auth_client.get(f"/sessions/{session_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["role_title"] == NEW_SESSION["role_title"]
    assert body["status"] == "active"
    assert body["turns"] == []
    assert body["scorecard"] is None


async def test_list_is_scoped_to_the_caller(auth_client, other_client):
    await _create(auth_client)
    await _create(auth_client)
    await _create(other_client)

    mine = await auth_client.get("/sessions")
    theirs = await other_client.get("/sessions")
    assert len(mine.json()) == 2
    assert len(theirs.json()) == 1


async def test_invalid_seniority_rejected(auth_client):
    resp = await auth_client.post(
        "/sessions", json={**NEW_SESSION, "seniority": "principal-emperor"}
    )
    assert resp.status_code == 422


async def test_too_many_focus_areas_rejected(auth_client):
    resp = await auth_client.post(
        "/sessions", json={**NEW_SESSION, "focus_areas": [f"area{i}" for i in range(20)]}
    )
    assert resp.status_code == 422


# --- Tenancy isolation ----------------------------------------------------


async def test_cannot_read_another_users_session(auth_client, other_client):
    """The IDOR test.

    404 rather than 403 is deliberate: a 403 confirms the id exists, which
    hands an attacker a working oracle for enumerating the id space.
    """
    session_id = await _create(auth_client)
    resp = await other_client.get(f"/sessions/{session_id}")
    assert resp.status_code == 404


async def test_cannot_delete_another_users_session(auth_client, other_client):
    session_id = await _create(auth_client)
    assert (await other_client.delete(f"/sessions/{session_id}")).status_code == 404
    # Still there for the real owner.
    assert (await auth_client.get(f"/sessions/{session_id}")).status_code == 200


async def test_cannot_answer_into_another_users_session(auth_client, other_client):
    session_id = await _create(auth_client)
    resp = await other_client.post(
        f"/sessions/{session_id}/answers", json={"content": "injected answer"}
    )
    assert resp.status_code == 404


async def test_cannot_stream_another_users_session(auth_client, other_client):
    session_id = await _create(auth_client)
    resp = await other_client.get(f"/sessions/{session_id}/stream")
    assert resp.status_code == 404


async def test_unauthenticated_requests_rejected(client):
    assert (await client.get("/sessions")).status_code == 401
    assert (await client.post("/sessions", json=NEW_SESSION)).status_code == 401


# --- Streaming ------------------------------------------------------------


async def test_stream_emits_start_tokens_and_done(auth_client):
    session_id = await _create(auth_client)
    events = await _consume_stream(auth_client, session_id)
    assert events[0] == "start"
    assert events[-1] == "done"
    assert events.count("token") > 3, "expected the question to arrive in multiple chunks"


async def test_streamed_question_is_persisted(auth_client):
    """The stream must not be fire-and-forget.

    The generator writes the completed turn in its own session, after the
    request-scoped one has already been handed off. This asserts that actually
    lands in the database.
    """
    session_id = await _create(auth_client)
    await _consume_stream(auth_client, session_id)

    detail = (await auth_client.get(f"/sessions/{session_id}")).json()
    assert len(detail["turns"]) == 1
    assert detail["turns"][0]["speaker"] == "interviewer"
    assert detail["turns"][0]["index"] == 0
    assert len(detail["turns"][0]["content"]) > 20


async def test_cost_accumulates_across_turns(auth_client):
    session_id = await _create(auth_client)
    before = (await auth_client.get(f"/sessions/{session_id}/cost")).json()
    assert before["spent_usd"] == 0.0

    await _consume_stream(auth_client, session_id)

    after = (await auth_client.get(f"/sessions/{session_id}/cost")).json()
    assert after["spent_usd"] > 0
    assert after["remaining_usd"] < after["ceiling_usd"]


async def test_turn_indices_alternate_and_increment(auth_client):
    session_id = await _create(auth_client)
    await _consume_stream(auth_client, session_id)
    await auth_client.post(
        f"/sessions/{session_id}/answers",
        json={"content": "I built a festival platform that served 1.9M users with no downtime."},
    )
    await _consume_stream(auth_client, session_id)

    turns = (await auth_client.get(f"/sessions/{session_id}")).json()["turns"]
    assert [t["index"] for t in turns] == [0, 1, 2]
    assert [t["speaker"] for t in turns] == ["interviewer", "candidate", "interviewer"]


async def test_answer_length_is_bounded(auth_client):
    session_id = await _create(auth_client)
    resp = await auth_client.post(
        f"/sessions/{session_id}/answers", json={"content": "x" * 9000}
    )
    assert resp.status_code == 422


async def test_empty_answer_rejected(auth_client):
    session_id = await _create(auth_client)
    resp = await auth_client.post(f"/sessions/{session_id}/answers", json={"content": ""})
    assert resp.status_code == 422


# --- Completion and scoring ----------------------------------------------


async def test_complete_returns_validated_scorecard(auth_client):
    session_id = await _create(auth_client)
    await _consume_stream(auth_client, session_id)
    await auth_client.post(
        f"/sessions/{session_id}/answers",
        json={"content": "We kept the site up through the ticket release. Zero downtime."},
    )

    resp = await auth_client.post(f"/sessions/{session_id}/complete")
    assert resp.status_code == 200, resp.text
    card = resp.json()

    assert 0 <= card["overall"] <= 100
    assert card["summary"]
    assert len(card["dimensions"]) == 5
    for dim in card["dimensions"]:
        assert 1 <= dim["score"] <= 5, "score bounds are enforced in code, not just the prompt"
        assert dim["name"] in {
            "Technical depth",
            "Structure",
            "Specificity",
            "Trade-off reasoning",
            "Communication",
        }


async def test_cannot_complete_without_answering(auth_client):
    session_id = await _create(auth_client)
    await _consume_stream(auth_client, session_id)
    resp = await auth_client.post(f"/sessions/{session_id}/complete")
    assert resp.status_code == 400


async def test_completed_session_rejects_new_answers(auth_client):
    session_id = await _create(auth_client)
    await _consume_stream(auth_client, session_id)
    await auth_client.post(f"/sessions/{session_id}/answers", json={"content": "An answer."})
    await auth_client.post(f"/sessions/{session_id}/complete")

    resp = await auth_client.post(f"/sessions/{session_id}/answers", json={"content": "Another."})
    assert resp.status_code == 409


async def test_completing_twice_is_idempotent(auth_client):
    session_id = await _create(auth_client)
    await _consume_stream(auth_client, session_id)
    await auth_client.post(f"/sessions/{session_id}/answers", json={"content": "An answer."})

    first = await auth_client.post(f"/sessions/{session_id}/complete")
    second = await auth_client.post(f"/sessions/{session_id}/complete")
    assert first.status_code == second.status_code == 200
    assert first.json()["overall"] == second.json()["overall"]


async def test_cannot_complete_another_users_session(auth_client, other_client):
    session_id = await _create(auth_client)
    await _consume_stream(auth_client, session_id)
    await auth_client.post(f"/sessions/{session_id}/answers", json={"content": "An answer."})
    assert (await other_client.post(f"/sessions/{session_id}/complete")).status_code == 404


# --- Ops ------------------------------------------------------------------
#
# This used to be one test against one endpoint, because /health used to check
# the database and return 503 when it was down. Used as a liveness probe --
# which is what the name invites -- that turns a database blip into a restart
# loop across every replica at once.
#
# The probes are split now and the full coverage lives in test_observability.py.
# What stays here is the assertion that they are still two different things,
# because the failure mode of quietly re-merging them is an outage rather than
# a red test.


async def test_readiness_reports_database_and_provider(client):
    resp = await client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["database"] == "up"


async def test_liveness_is_not_readiness(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert "database" not in resp.json(), (
        "a dependency check crept back into the liveness probe"
    )
