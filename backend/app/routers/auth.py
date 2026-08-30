"""Authentication routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.deps import CurrentUser, DbSession
from app.models import User
from app.ratelimit import login_limit, register_limit
from app.schemas import RefreshRequest, TokenPair, UserCreate, UserLogin, UserOut
from app.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# One hash at process startup, rather than generating a new expensive bcrypt
# hash for every unknown email before doing the comparison.
DUMMY_PASSWORD_HASH = hash_password("not-a-real-password")


def _tokens_for(user: User) -> TokenPair:
    return TokenPair(
        access_token=create_access_token(user.id),
        refresh_token=create_refresh_token(user.id),
        expires_in=settings.access_token_ttl_minutes * 60,
    )


@router.post(
    "/register",
    response_model=TokenPair,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(register_limit)],
)
async def register(payload: UserCreate, db: DbSession) -> TokenPair:
    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        # Deliberately the same shape as any other registration failure so the
        # endpoint cannot be used to enumerate which emails have accounts.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Could not create that account",
        ) from None
    return _tokens_for(user)


@router.post("/login", response_model=TokenPair, dependencies=[Depends(login_limit)])
async def login(payload: UserLogin, db: DbSession) -> TokenPair:
    result = await db.execute(select(User).where(User.email == payload.email.lower()))
    user = result.scalar_one_or_none()

    # Verify against a dummy hash when the user is missing so the response time
    # does not reveal whether the address exists.
    if user is None:
        verify_password(payload.password, DUMMY_PASSWORD_HASH)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )

    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password"
        )
    return _tokens_for(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenPair:
    try:
        user_id = decode_token(payload.refresh_token, expected_type="refresh")
    except TokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        ) from None

    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )
    return _tokens_for(user)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    # Returns the ORM object; response_model=UserOut is what guarantees the
    # password hash cannot travel with it.
    return user
