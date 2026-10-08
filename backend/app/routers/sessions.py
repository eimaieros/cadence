"""Interview session routes, including the streaming endpoint.

A note on authenticating the stream, since it comes up every time: the browser
`EventSource` API cannot send an `Authorization` header. The usual workarounds
are to put the token in the query string -- which lands it in access logs,
proxy logs and browser history -- or to issue a short-lived ticket. This
service does neither: the front end consumes the stream with `fetch` and a
`ReadableStream`, which supports headers normally. Same wire format, same
`text/event-stream` content type, no token in a URL.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select

from app.config import settings
from app.deps import CurrentUser, DbSession, OwnedSession
from app.llm.client import LLMError, StreamResult, Usage
from app.llm.prompts import interviewer_context, interviewer_system
from app.llm.scoring import ScoringError, score_transcript
from app.llm.tools import (
    MAX_CALLS_PER_ROUND,
    MAX_TOOL_ROUNDS,
    ToolContext,
    ToolOutcome,
    run_tool,
    tool_specs,
)
from app.models import InterviewSession, Scorecard, SessionStatus, Speaker, ToolCall, Turn
from app.ratelimit import session_create_limit, stream_limit
from app.schemas import (
    AnswerCreate,
    CostState,
    ScorecardOut,
    SessionCreate,
    SessionDetail,
    SessionSummary,
    ToolCallOut,
    TurnOut,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["sessions"])

MAX_QUESTIONS = 6


def _advisory_key(session_id) -> int:
    """Stable signed 64-bit key for PostgreSQL's transaction advisory lock."""
    return int.from_bytes(
        hashlib.blake2b(session_id.bytes, digest_size=8).digest(),
        byteorder="big",
        signed=True,
    )


async def _lock_session(db, session_id) -> None:
    """Refuse overlapping mutations across workers instead of racing them.

    The transaction-scoped lock releases automatically on commit, rollback or
    disconnect. ``try`` is intentional: an API request gets an actionable 409
    rather than waiting behind an LLM call for an unbounded amount of time.
    """
    locked = await db.scalar(select(func.pg_try_advisory_xact_lock(_advisory_key(session_id))))
    if not locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another operation is already running for this session.",
        )


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event.

    The blank line terminator is not optional -- without it the browser buffers
    the event and the stream appears to hang.
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _build_messages(turns: list[Turn]) -> list[dict]:
    """Map stored turns onto the model's message format.

    Interviewer turns are `assistant`, candidate turns are `user`. Candidate
    text only ever occupies a user turn -- it is never concatenated into the
    system prompt, which is the structural half of the injection defence.
    """
    return [
        {
            "role": "assistant" if t.speaker == Speaker.interviewer else "user",
            "content": t.content,
        }
        for t in turns
    ]


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


@router.post(
    "",
    response_model=SessionSummary,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(session_create_limit)],
)
async def create_session(
    payload: SessionCreate, user: CurrentUser, db: DbSession
) -> InterviewSession:
    session = InterviewSession(
        user_id=user.id,
        role_title=payload.role_title,
        focus_areas=payload.focus_areas,
        seniority=payload.seniority,
    )
    db.add(session)
    await db.flush()
    return session


@router.get("", response_model=list[SessionSummary])
async def list_sessions(
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[InterviewSession]:
    stmt = (
        select(InterviewSession)
        .where(InterviewSession.user_id == user.id)
        .order_by(desc(InterviewSession.created_at))
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(session: OwnedSession) -> InterviewSession:
    # Ownership was resolved by the dependency. Nothing to check here, which is
    # the point -- there is no path where a handler forgets.
    return session


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(session: OwnedSession, db: DbSession) -> None:
    await _lock_session(db, session.id)
    await db.delete(session)


@router.get("/{session_id}/tool-calls", response_model=list[ToolCallOut])
async def list_tool_calls(session: OwnedSession, db: DbSession) -> list[ToolCall]:
    """The interviewer's tool audit for one session, oldest first.

    Ownership comes from the same dependency as every other session route, so a
    session that is not yours has no audit you can read -- it does not exist.
    """
    stmt = (
        select(ToolCall)
        .where(ToolCall.session_id == session.id)
        .order_by(ToolCall.turn_index, ToolCall.round, ToolCall.created_at)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/{session_id}/cost", response_model=CostState)
async def get_cost(session: OwnedSession) -> CostState:
    ceiling = settings.session_cost_ceiling_usd
    return CostState(
        spent_usd=round(session.cost_usd, 6),
        ceiling_usd=ceiling,
        remaining_usd=round(max(0.0, ceiling - session.cost_usd), 6),
    )


# --------------------------------------------------------------------------
# Conversation
# --------------------------------------------------------------------------


@router.post("/{session_id}/answers", response_model=TurnOut, status_code=status.HTTP_201_CREATED)
async def submit_answer(
    payload: AnswerCreate, session: OwnedSession, db: DbSession
) -> Turn:
    await _lock_session(db, session.id)
    await db.refresh(session, attribute_names=["status"])
    if session.status is not SessionStatus.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This session is already closed"
        )

    next_index = await db.scalar(
        select(func.coalesce(func.max(Turn.index), -1) + 1).where(Turn.session_id == session.id)
    )
    turn = Turn(
        session_id=session.id,
        index=next_index or 0,
        speaker=Speaker.candidate,
        content=payload.content,
    )
    db.add(turn)
    await db.flush()
    return turn


@router.get("/{session_id}/stream", dependencies=[Depends(stream_limit)])
async def stream_question(
    request: Request, session: OwnedSession, db: DbSession
) -> StreamingResponse:
    """Stream the interviewer's next question, token by token."""
    await _lock_session(db, session.id)
    await db.refresh(session, attribute_names=["status", "cost_usd", "turns"])
    if session.status is not SessionStatus.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This session is already closed"
        )

    # Stop starting interview calls once recorded spend reaches the cutoff.
    # This is a guardrail, not a reservation: the call already in flight can
    # cross it, and the final scoring call is deliberately still available.
    if session.cost_usd >= settings.session_cost_ceiling_usd:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="This session has reached its spend guardrail. End it to see your scorecard.",
        )

    turns = list(session.turns)
    asked = sum(1 for t in turns if t.speaker == Speaker.interviewer)
    if asked >= MAX_QUESTIONS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This interview has run its course. End it to see your scorecard.",
        )

    provider = request.app.state.llm
    system = interviewer_system(MAX_QUESTIONS)
    messages = [
        {
            "role": "user",
            "content": interviewer_context(
                session.role_title, session.seniority, session.focus_areas
            ),
        },
        *_build_messages(turns),
    ]

    next_index = (max((t.index for t in turns), default=-1)) + 1
    session_id = session.id
    model = settings.llm_model_interviewer

    # Tools are offered only to providers that declare support. The context is
    # fixed here, on the server, before the model runs: no tool takes an id, so
    # nothing the model writes can point a call at another session.
    uses_tools = bool(getattr(provider, "supports_tools", False))
    tool_ctx = ToolContext(
        focus_areas=list(session.focus_areas or []),
        transcript=[t.content for t in turns],
        questions_asked=asked,
        max_questions=MAX_QUESTIONS,
    )
    spent_before = session.cost_usd or 0.0

    async def generator() -> AsyncIterator[str]:
        from app.db import SessionFactory

        total = Usage(model=model)
        visible: list[str] = []
        convo = list(messages)
        try:
            yield _sse("start", {"index": next_index})

            rounds = MAX_TOOL_ROUNDS if uses_tools else 0
            for round_no in range(rounds + 1):
                # The last round offers no tools, so a model that keeps asking
                # for them still has to write the question. Once recorded spend
                # reaches the guardrail, no more rounds are offered either.
                offer = None
                if (
                    round_no < rounds
                    and spent_before + total.cost_usd < settings.session_cost_ceiling_usd
                ):
                    offer = tool_specs()

                result = StreamResult()
                extra = {"tools": offer} if uses_tools else {}
                gone = False
                async for piece in provider.stream(
                    system=system, messages=convo, model=model, result=result, **extra
                ):
                    # If the client navigated away, stop paying for tokens nobody
                    # will read.
                    if await request.is_disconnected():
                        logger.info("client disconnected mid-stream, aborting")
                        gone = True
                        break
                    visible.append(piece)
                    yield _sse("token", {"text": piece})
                if gone:
                    yield _sse("error", {"detail": "The interviewer produced no response."})
                    return

                total = Usage(
                    model=model,
                    input_tokens=total.input_tokens + result.usage.input_tokens,
                    output_tokens=total.output_tokens + result.usage.output_tokens,
                )
                calls = result.tool_calls if offer else []
                if not calls:
                    break

                # Every tool_use needs a tool_result, so calls past the cap are
                # answered with an error instead of being dropped.
                assistant_blocks: list[dict] = []
                if result.text:
                    assistant_blocks.append({"type": "text", "text": result.text})
                tool_results: list[dict] = []
                for n, call in enumerate(calls):
                    assistant_blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.id,
                            "name": call.name,
                            "input": call.input if call.input is not None else {},
                        }
                    )
                    if n < MAX_CALLS_PER_ROUND:
                        yield _sse("tool", {"name": call.name[:64]})
                        outcome = await run_tool(call.name, call.input, tool_ctx)
                    else:
                        outcome = ToolOutcome(
                            False, {"error": f"at most {MAX_CALLS_PER_ROUND} tool calls per turn"}
                        )
                    async with SessionFactory() as write:
                        async with write.begin():
                            write.add(
                                ToolCall(
                                    session_id=session_id,
                                    turn_index=next_index,
                                    round=round_no,
                                    name=call.name[:64],
                                    arguments=(
                                        call.input
                                        if call.input is not None
                                        else {"unparsed": call.raw[:2000]}
                                    ),
                                    result=outcome.result,
                                    ok=outcome.ok,
                                )
                            )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": call.id,
                            "content": json.dumps(outcome.result, ensure_ascii=False),
                            "is_error": not outcome.ok,
                        }
                    )
                convo = [
                    *convo,
                    {"role": "assistant", "content": assistant_blocks},
                    {"role": "user", "content": tool_results},
                ]

            text = "".join(visible).strip()
            if not text:
                yield _sse("error", {"detail": "The interviewer produced no response."})
                return

            # Persist in a fresh session: the request-scoped one is tied to the
            # dependency's lifecycle, which has already handed control to the
            # streaming response by the time we get here.
            async with SessionFactory() as write:
                async with write.begin():
                    turn = Turn(
                        session_id=session_id,
                        index=next_index,
                        speaker=Speaker.interviewer,
                        content=text,
                        input_tokens=total.input_tokens,
                        output_tokens=total.output_tokens,
                        cost_usd=total.cost_usd,
                    )
                    write.add(turn)
                    live = await write.get(InterviewSession, session_id)
                    if live is not None:
                        live.cost_usd = (live.cost_usd or 0.0) + total.cost_usd

            yield _sse(
                "done",
                {
                    "index": next_index,
                    "content": text,
                    "cost_usd": round(total.cost_usd, 6),
                },
            )
        except LLMError as exc:
            logger.exception("stream failed")
            yield _sse("error", {"detail": str(exc)})
        except Exception:
            # Never let an internal error escape as a stack trace to the client.
            logger.exception("unexpected stream failure")
            yield _sse("error", {"detail": "The interviewer is unavailable right now."})

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Without this, nginx buffers the whole response and the stream
            # arrives as one lump at the end -- which looks exactly like a
            # broken stream and is maddening to debug.
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{session_id}/complete", response_model=ScorecardOut)
async def complete_session(
    request: Request, session: OwnedSession, db: DbSession
) -> Scorecard:
    await _lock_session(db, session.id)
    await db.refresh(session, attribute_names=["status", "scorecard", "turns"])
    if session.status is SessionStatus.completed and session.scorecard is not None:
        return session.scorecard

    turns = list(session.turns)
    if len([t for t in turns if t.speaker == Speaker.candidate]) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Answer at least one question before ending the session.",
        )

    try:
        draft, usage = await score_transcript(request.app.state.llm, turns)
    except ScoringError as exc:
        logger.error("scoring failed for session %s: %s", session.id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not produce a scorecard. Your transcript is saved -- try again.",
        ) from exc

    card = Scorecard(
        session_id=session.id,
        overall=draft.overall,
        summary=draft.summary,
        dimensions=[d.model_dump() for d in draft.dimensions],
        strengths=draft.strengths,
        gaps=draft.gaps,
    )
    db.add(card)
    session.status = SessionStatus.completed
    session.completed_at = datetime.now(UTC)
    session.cost_usd = (session.cost_usd or 0.0) + usage.cost_usd
    await db.flush()
    return card
