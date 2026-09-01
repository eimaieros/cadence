"""Password hashing and token handling.

Two token types share a signing key but carry a `typ` claim, and each is only
accepted where it belongs. Without that, a refresh token -- which is long-lived
by design -- would be usable as an access token, which quietly turns a 30 minute
exposure window into a 14 day one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import bcrypt
import jwt

from app.config import settings

# bcrypt directly rather than passlib. passlib has had no release since 2020 and
# its bcrypt backend breaks against bcrypt >= 4.1, which now raises on over-long
# input instead of silently truncating. One less unmaintained dependency in the
# authentication path is worth the handful of lines.
BCRYPT_ROUNDS = 12

TokenType = Literal["access", "refresh"]


class TokenError(Exception):
    """Raised when a token is missing, malformed, expired, or the wrong type."""


@dataclass(frozen=True)
class TokenClaims:
    """The validated identity and lifecycle fields carried by a token."""

    subject: uuid.UUID
    jti: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class IssuedToken:
    """A signed token plus the metadata needed for server-side rotation."""

    value: str
    jti: uuid.UUID
    expires_at: datetime


def hash_password(plain: str) -> str:
    """Hash a password, rejecting input bcrypt cannot represent.

    bcrypt only considers the first 72 bytes. Truncating silently would mean two
    different long passwords hash identically, so this refuses instead. The
    schema checks both characters and UTF-8 bytes for real users -- this is the
    backstop for internal callers.
    """
    raw = plain.encode("utf-8")
    if len(raw) > 72:
        raise ValueError("password exceeds 72 bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time comparison via bcrypt; never raises on malformed input."""
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def _issue(subject: uuid.UUID, token_type: TokenType, ttl: timedelta) -> IssuedToken:
    now = datetime.now(UTC)
    expires_at = now + ttl
    jti = uuid.uuid4()
    payload: dict[str, Any] = {
        "sub": str(subject),
        "typ": token_type,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "jti": str(jti),
    }
    return IssuedToken(
        value=jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm),
        jti=jti,
        expires_at=expires_at,
    )


def create_access_token(user_id: uuid.UUID) -> str:
    return _issue(
        user_id, "access", timedelta(minutes=settings.access_token_ttl_minutes)
    ).value


def create_refresh_token(user_id: uuid.UUID) -> str:
    return issue_refresh_token(user_id).value


def issue_refresh_token(user_id: uuid.UUID) -> IssuedToken:
    """Issue a refresh token and expose only its non-secret lifecycle metadata."""
    return _issue(user_id, "refresh", timedelta(days=settings.refresh_token_ttl_days))


def decode_token(token: str, expected_type: TokenType) -> uuid.UUID:
    """Return the subject, or raise TokenError.

    Deliberately returns one opaque error for every failure mode. Telling a
    caller the difference between "expired" and "bad signature" is free
    reconnaissance.
    """
    return decode_token_claims(token, expected_type).subject


def decode_token_claims(token: str, expected_type: TokenType) -> TokenClaims:
    """Validate a token and return the claims rotation needs."""
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "jti", "sub", "typ"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError("invalid token") from exc

    if payload.get("typ") != expected_type:
        raise TokenError("invalid token")

    try:
        return TokenClaims(
            subject=uuid.UUID(payload["sub"]),
            jti=uuid.UUID(payload["jti"]),
            expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise TokenError("invalid token") from exc
