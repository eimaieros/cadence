"""ORM models.

Design notes worth defending out loud:

* Every row that belongs to a person carries `user_id`, and every query for it
  goes through a helper that filters on the authenticated user. Ownership is
  not something each route handler is trusted to remember.
* Primary keys are UUIDs rather than sequential integers. That is defence in
  depth, not an access control -- authorisation is still checked explicitly --
  but it removes the "increment the id and see what happens" class of probing.
* Money is tracked in `cost_usd` per turn and summed per session so new
  interview calls can be stopped at the configured spend guardrail.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class SessionStatus(str, enum.Enum):
    active = "active"
    completed = "completed"
    abandoned = "abandoned"


class Speaker(str, enum.Enum):
    interviewer = "interviewer"
    candidate = "candidate"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    # Never exposed. The response_model on every route excludes it structurally
    # rather than relying on anyone remembering to strip it.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    sessions: Mapped[list["InterviewSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )


class RefreshToken(Base):
    """One rotating refresh credential.

    Only the random JWT identifier is stored, never the signed bearer token.
    Every login starts a family; each refresh consumes one row and creates its
    successor in the same family. Reuse revokes the entire family.
    """

    __tablename__ = "refresh_tokens"

    token_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    family_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_refresh_tokens_user", "user_id"),
        Index("ix_refresh_tokens_family", "family_id"),
    )


class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_title: Mapped[str] = mapped_column(String(160), nullable=False)
    focus_areas: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    seniority: Mapped[str] = mapped_column(String(40), default="mid", nullable=False)
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status"), default=SessionStatus.active, nullable=False
    )
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions")
    turns: Mapped[list["Turn"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="Turn.index",
        lazy="selectin",
    )
    scorecard: Mapped["Scorecard | None"] = relationship(
        back_populates="session", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )

    __table_args__ = (
        # The composite order matters: every listing query filters by user_id
        # first and sorts by created_at, so user_id has to be the leading column.
        Index("ix_sessions_user_created", "user_id", "created_at"),
        CheckConstraint("cost_usd >= 0", name="ck_sessions_cost_non_negative"),
    )


class Turn(Base):
    __tablename__ = "turns"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("interview_sessions.id", ondelete="CASCADE"), nullable=False
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    speaker: Mapped[Speaker] = mapped_column(Enum(Speaker, name="speaker"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped[InterviewSession] = relationship(back_populates="turns")

    __table_args__ = (
        Index("ix_turns_session_index", "session_id", "index", unique=True),
    )


class Scorecard(Base):
    __tablename__ = "scorecards"

    id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    overall: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    # Dimensions arrive as validated JSON from the model. Stored as JSONB so it
    # is queryable later (e.g. "show me every session where structure < 3")
    # without a migration per new dimension.
    dimensions: Mapped[list[dict]] = mapped_column(JSONB, default=list, nullable=False)
    strengths: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    gaps: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped[InterviewSession] = relationship(back_populates="scorecard")

    __table_args__ = (
        CheckConstraint("overall >= 0 AND overall <= 100", name="ck_scorecard_overall_range"),
    )
