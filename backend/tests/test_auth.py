"""Authentication tests."""

from __future__ import annotations

import pytest

from app.security import create_refresh_token

pytestmark = pytest.mark.asyncio


async def test_register_returns_tokens(client):
    resp = await client.post(
        "/auth/register",
        json={
            "email": "new@example.com",
            "password": "a-long-enough-password",
            "display_name": "New Person",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] and body["refresh_token"]


async def test_password_hash_never_leaves_the_api(auth_client):
    """The response_model is the control. This test is what proves it holds."""
    resp = await auth_client.get("/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert "password_hash" not in body
    assert "password" not in body
    assert body["email"] == "rodrigo@example.com"


async def test_duplicate_email_does_not_confirm_existence(client):
    payload = {
        "email": "dup@example.com",
        "password": "a-long-enough-password",
        "display_name": "Dup",
    }
    assert (await client.post("/auth/register", json=payload)).status_code == 201
    second = await client.post("/auth/register", json=payload)
    assert second.status_code == 409
    # Generic wording -- must not say "this email is already registered".
    assert "already" not in second.json()["detail"].lower()


async def test_login_wrong_password_is_rejected(client):
    await client.post(
        "/auth/register",
        json={
            "email": "login@example.com",
            "password": "a-long-enough-password",
            "display_name": "Login",
        },
    )
    resp = await client.post(
        "/auth/login", json={"email": "login@example.com", "password": "wrong-password-here"}
    )
    assert resp.status_code == 401


async def test_login_unknown_email_gives_same_error(client):
    resp = await client.post(
        "/auth/login", json={"email": "ghost@example.com", "password": "wrong-password-here"}
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


async def test_short_password_rejected(client):
    resp = await client.post(
        "/auth/register",
        json={"email": "short@example.com", "password": "abc", "display_name": "Short"},
    )
    assert resp.status_code == 422


async def test_refresh_token_cannot_be_used_as_access_token(client):
    """Token type confusion.

    Without the `typ` claim check, a refresh token -- valid for two weeks --
    would authenticate requests that should require a 30 minute access token.
    """
    resp = await client.post(
        "/auth/register",
        json={
            "email": "typ@example.com",
            "password": "a-long-enough-password",
            "display_name": "Typ",
        },
    )
    refresh = resp.json()["refresh_token"]

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert me.status_code == 401


async def test_refresh_rotates_tokens(client):
    resp = await client.post(
        "/auth/register",
        json={
            "email": "rot@example.com",
            "password": "a-long-enough-password",
            "display_name": "Rot",
        },
    )
    refreshed = await client.post(
        "/auth/refresh", json={"refresh_token": resp.json()["refresh_token"]}
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"]


async def test_garbage_token_rejected(client):
    resp = await client.get("/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


async def test_missing_auth_header_rejected(client):
    assert (await client.get("/auth/me")).status_code == 401


async def test_token_for_deleted_user_rejected(client):
    import uuid

    token = create_refresh_token(uuid.uuid4())
    resp = await client.post("/auth/refresh", json={"refresh_token": token})
    assert resp.status_code == 401
