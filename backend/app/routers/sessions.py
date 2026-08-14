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

import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, func, select

from app.config import settings
from app.deps import CurrentUser, DbSession, OwnedSession
from app.llm.client import LLMError, StreamResult
from app.llm.prompts import interviewer_system
from app.llm.scoring import ScoringError, score_transcript
from app.models import InterviewSession, Scorecard, SessionStatus, Speaker, Turn
from app.ratelimit import session_create_limit, stream_limit
from app.schemas import (
    AnswerCreate,
    CostState,
    ScorecardOut,
    SessionCreate,
    SessionDetail,
    SessionSummary,
    TurnOut,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sessions", tags=["sessions"])

MAX_QUESTIONS = 6


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
    user: CurrentUser, db: DbSession, limit: int = 20, offset: int = 0
) -> list[InterviewSession]:
    limit = max(1, min(limit, 100))
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
    await db.delete(session)


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
    if session.status is not SessionStatus.active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="This session is already closed"
        )

    # Enforce the ceiling BEFORE spending, not after. Checking afterwards means
    # the limit is a report rather than a control.
    if session.cost_usd >= settings.session_cost_ceiling_usd:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="This session has reached its cost ceiling. End it to see your scorecard.",
        )

    turns = list(session.turns)
    asked = sum(1 for t in turns if t.speaker == Speaker.interviewer)
    if asked > MAX_QUESTIONS:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This interview has run its course. End it to see your scorecard.",
        )

    provider = request.app.state.llm
    system = interviewer_system(
        session.role_title, session.seniority, session.focus_areas, MAX_QUESTIONS
    )
    messages = _build_messages(turns)
    if not messages:
        messages = [{"role": "user", "content": "I'm ready to start."}]

    next_index = (max((t.index for t in turns), default=-1)) + 1
    session_id = session.id

    async def generator() -> AsyncIterator[str]:
        result = StreamResult()
        try:
            yield _sse("start", {"index": next_index})

            async for piece in provider.stream(
                system=system,
                messages=messages,
                model=settings.llm_model_interviewer,
                result=result,
            ):
                # If the client navigated away, stop paying for tokens nobody
                # will read.
                if await request.is_disconnected():
                    logger.info("client disconnected mid-stream, aborting")
                    break
                yield _sse("token", {"text": piece})

            if not result.text:
                yield _sse("error", {"detail": "The interviewer produced no response."})
                return

            # Persist in a fresh session: the request-scoped one is tied to the
            # dependency's lifecycle, which has already handed control to the
            # streaming response by the time we get here.
            from app.db import SessionFactory

            async with SessionFactory() as write:
                async with write.begin():
                    turn = Turn(
                        session_id=session_id,
                        index=next_index,
                        speaker=Speaker.interviewer,
                        content=result.text,
                        input_tokens=result.usage.input_tokens,
                        output_tokens=result.usage.output_tokens,
                        cost_usd=result.usage.cost_usd,
                    )
                    write.add(turn)
                    live = await write.get(InterviewSession, session_id)
                    if live is not None:
                        live.cost_usd = (live.cost_usd or 0.0) + result.usage.cost_usd

            yield _sse(
                "done",
                {
                    "index": next_index,
                    "content": result.text,
                    "cost_usd": round(result.usage.cost_usd, 6),
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
