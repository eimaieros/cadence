"""Which address an anonymous rate limit is keyed on, behind an edge and without one.

Deployed, every request reaches the API through CloudFront and a reverse proxy,
so the TCP peer is always the proxy. Keyed on that, the five registrations the
limiter allows in five minutes would be five for everyone on the internet. The
edge writes the viewer's address into a header; these tests pin down when that
header is believed and how it is read.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from app.config import Settings, settings
from app.ratelimit import client_key, register_limit, viewer_address

EDGE = "cloudfront-viewer-address"


def _request(headers: dict[str, str] | None = None, peer: str = "10.0.0.5") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/auth/login",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": (peer, 51234),
        }
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("203.0.113.7:46532", "203.0.113.7"),
        ("2001:db8:aa:bb::1:46532", "2001:db8:aa:bb::/64"),
        # A four-digit port is also a valid hextet. Read greedily, this would be
        # one IPv6 address per connection and the limit would never bind.
        ("2001:db8::1:8080", "2001:db8::/64"),
        ("[2001:db8::1]:443", "2001:db8::/64"),
    ],
)
def test_viewer_address_reads_the_edge_format(raw, expected):
    assert viewer_address(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "203.0.113.7", "not-an-address:80", "203.0.113.7:http", "999.1.1.1:80"]
)
def test_viewer_address_refuses_what_it_cannot_read(raw):
    assert viewer_address(raw) is None


def test_the_header_is_ignored_unless_configured(monkeypatch):
    """Locally there is no edge, and a client can send any header it likes."""
    monkeypatch.setattr(settings, "client_ip_header", None)
    request = _request({"CloudFront-Viewer-Address": "203.0.113.7:46532"})
    assert client_key(request) == "ip:10.0.0.5"


def test_a_configured_edge_header_replaces_the_proxy_address(monkeypatch):
    monkeypatch.setattr(settings, "client_ip_header", EDGE)
    request = _request({"CloudFront-Viewer-Address": "203.0.113.7:46532"})
    assert client_key(request) == "ip:203.0.113.7"


def test_an_unreadable_edge_header_falls_back_to_the_peer(monkeypatch):
    monkeypatch.setattr(settings, "client_ip_header", EDGE)
    assert client_key(_request({"CloudFront-Viewer-Address": "garbage"})) == "ip:10.0.0.5"
    assert client_key(_request()) == "ip:10.0.0.5"


def test_a_token_still_wins_over_any_address(monkeypatch):
    """Signed-in limits follow the account, wherever it connects from."""
    monkeypatch.setattr(settings, "client_ip_header", EDGE)
    request = _request(
        {"Authorization": "Bearer abc", "CloudFront-Viewer-Address": "203.0.113.7:1"}
    )
    assert client_key(request).startswith("token:")


def test_header_names_are_validated_and_normalised():
    configured = Settings(client_ip_header="CloudFront-Viewer-Address", _env_file=None)
    assert configured.client_ip_header == EDGE
    assert Settings(client_ip_header="  ", _env_file=None).client_ip_header is None
    with pytest.raises(ValidationError):
        Settings(client_ip_header="X Forwarded For", _env_file=None)


async def test_viewers_behind_one_proxy_get_separate_budgets(client, monkeypatch):
    """Five registrations per five minutes per viewer -- not for the whole site."""
    monkeypatch.setattr(settings, "client_ip_header", EDGE)
    register_limit.limiter.reset()

    async def register(i: int, viewer: str) -> int:
        resp = await client.post(
            "/auth/register",
            json={
                "email": f"edge{i}@example.com",
                "password": "a-long-enough-password",
                "display_name": f"Edge {i}",
            },
            headers={"CloudFront-Viewer-Address": viewer},
        )
        return resp.status_code

    first = [await register(i, "203.0.113.7:40000") for i in range(6)]
    assert first == [201] * 5 + [429]
    assert await register(99, "198.51.100.20:40001") == 201
