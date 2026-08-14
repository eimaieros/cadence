"""Request and response schemas.

The response models here are a security control, not documentation. `UserOut`
has no `password_hash` field, so the hash cannot leak through a route that
returns a `User` -- FastAPI serialises through the declared `response_model`
and drops anything not on it. That is structural, rather than depending on
every handler remembering to strip it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models import SessionStatus, Speaker

# --- Auth -----------------------------------------------------------------


class UserCreate(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=10, max_length=72)]
    display_name: Annotated[str, Field(min_length=1, max_length=120)]

    @field_validator("password")
    @classmethod
    def _not_obviously_weak(cls, v: str) -> str:
        # A deliberately small check. Real strength policy belongs in a shared
        # rule set with the client; length is the part that actually matters.
        if v.lower() in {"password12", "passwordpassword", "1234567890"}:
            raise ValueError("password is too common")
        return v


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str
    created_at: datetime


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


# --- Sessions -------------------------------------------------------------

SENIORITIES = ("junior", "mid", "senior", "staff")


class SessionCreate(BaseModel):
    role_title: Annotated[str, Field(min_length=2, max_length=160)]
    focus_areas: Annotated[list[str], Field(max_length=6)] = []
    seniority: Literal["junior", "mid", "senior", "staff"] = "mid"

    @field_validator("focus_areas")
    @classmethod
    def _clean(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        if any(len(s) > 60 for s in cleaned):
            raise ValueError("focus areas must be 60 characters or fewer")
        return cleaned


class TurnOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    index: int
    speaker: Speaker
    content: str
    created_at: datetime


class AnswerCreate(BaseModel):
    content: Annotated[str, Field(min_length=1, max_length=8000)]


class DimensionScore(BaseModel):
    """One row of the rubric. Also the schema the model is told to produce."""

    name: str
    score: Annotated[int, Field(ge=1, le=5)]
    note: Annotated[str, Field(max_length=400)]


class ScorecardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    overall: Annotated[int, Field(ge=0, le=100)]
    summary: str
    dimensions: list[DimensionScore]
    strengths: list[str]
    gaps: list[str]
    created_at: datetime


class SessionSummary(BaseModel):
    """Listing shape -- deliberately excludes turns so the list endpoint stays
    cheap. Fetching every transcript to render a list is the classic N+1 that
    only hurts once there is real data."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role_title: str
    seniority: str
    focus_areas: list[str]
    status: SessionStatus
    cost_usd: float
    created_at: datetime
    completed_at: datetime | None


class SessionDetail(SessionSummary):
    turns: list[TurnOut]
    scorecard: ScorecardOut | None = None


class CostState(BaseModel):
    spent_usd: float
    ceiling_usd: float
    remaining_usd: float


class ErrorOut(BaseModel):
    detail: str
