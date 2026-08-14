"""Shared dependencies.

`get_owned_session` is the important one. Every route that touches a session
resolves it through here, so the ownership check happens once in a single place
instead of being re-implemented -- and eventually forgotten -- in each handler.
That forgetting is exactly how IDOR bugs ship.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Path, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import InterviewSession, User
from app.security import TokenError, decode_token

bearer = HTTPBearer(auto_error=False)

CREDENTIALS_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    if creds is None or not creds.credentials:
        raise CREDENTIALS_ERROR
    try:
        user_id = decode_token(creds.credentials, expected_type="access")
    except TokenError:
        raise CREDENTIALS_ERROR from None

    user = await db.get(User, user_id)
    if user is None:
        # Token signature was valid but the user is gone -- deleted account with
        # a token still in the wild. Same opaque response.
        raise CREDENTIALS_ERROR
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


async def get_owned_session(
    session_id: Annotated[uuid.UUID, Path()],
    user: CurrentUser,
    db: DbSession,
) -> InterviewSession:
    """Load a session that belongs to the caller.

    Note the 404 rather than 403 on a session owned by someone else. A 403
    confirms the resource exists, which leaks the id space to anyone probing.
    From the caller's perspective, a session they cannot see does not exist.
    """
    stmt = select(InterviewSession).where(
        InterviewSession.id == session_id,
        InterviewSession.user_id == user.id,
    )
    result = await db.execute(stmt)
    found = result.scalar_one_or_none()
    if found is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return found


OwnedSession = Annotated[InterviewSession, Depends(get_owned_session)]
