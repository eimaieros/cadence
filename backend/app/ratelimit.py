"""Per-user rate limiting.

Scope, stated plainly: this is an in-process sliding window. It bounds one
instance. Behind two replicas a caller gets twice the budget, and a restart
clears every window.

That is a deliberate trade, not an oversight. The expensive path here is already
guarded by a per-session recorded-spend cutoff in the database, which survives
both restarts and horizontal scaling. This limiter exists for
the cheaper abuse it does not cover -- registration spam, login brute force, and
someone opening sessions in a loop.

Moving to Redis is a swap of the `_hits` dict for a sorted set with the same
interface. The reason not to do that now is that it adds a service to the
compose file for a product with one instance, and an unnecessary dependency is
its own kind of bug.
"""

from __future__ import annotations

import ipaddress
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

from app.config import settings


class SlidingWindowLimiter:
    """Counts hits per key inside a moving time window.

    A fixed window lets a caller fire the full budget at 0:59 and again at 1:01
    -- double the intended rate across the boundary. A sliding window costs a
    deque per key and does not have that edge.
    """

    def __init__(self, limit: int, window_seconds: float) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if not isinstance(window_seconds, (int, float)) or window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._checks = 0

    def _sweep(self, now: float) -> None:
        """Drop inactive identities so random bearer tokens cannot grow RAM forever."""
        stale = [
            key for key, hits in self._hits.items()
            if not hits or now - hits[-1] > self.window
        ]
        for key in stale:
            self._hits.pop(key, None)

    def check(self, key: str) -> tuple[bool, float]:
        """Return (allowed, seconds_until_retry)."""
        now = time.monotonic()
        self._checks += 1
        if self._checks % 256 == 0:
            self._sweep(now)
        hits = self._hits[key]

        while hits and now - hits[0] > self.window:
            hits.popleft()

        if len(hits) >= self.limit:
            return False, max(0.0, self.window - (now - hits[0]))

        hits.append(now)
        return True, 0.0

    def reset(self) -> None:
        self._hits.clear()
        self._checks = 0


def viewer_address(value: str) -> str | None:
    """The address in an edge's "address:port" header, or None if it is not one.

    CloudFront writes `CloudFront-Viewer-Address` as `203.0.113.7:46532`, and an
    IPv6 viewer as the bare address followed by the port, without brackets. The
    port is always there, so the last colon is always the separator -- trying
    the whole value as an address first would read `2001:db8::1:8080` as one
    IPv6 address and key every connection of that viewer separately.

    IPv6 viewers are keyed on their /64. A single subscriber is routinely given
    a whole /64, and a limit per address would hand them 2**64 budgets.
    """
    host, sep, port = value.strip().rpartition(":")
    if not sep or not port.isdigit():
        return None
    try:
        address = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None
    if address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def client_key(request: Request) -> str:
    """Identify the caller.

    Authenticated requests key on the token, so a limit follows the account
    rather than the network. Anonymous ones key on an address: the one a
    trusted edge wrote into `client_ip_header`, when that is configured and
    parses, and the TCP peer otherwise.

    The peer alone is coarse behind a proxy -- it is the proxy -- and trusting
    a client-writable `X-Forwarded-For` instead is how a limiter becomes
    decorative. The edge header is trusted only because the deployment makes
    the edge the only way in (see infra/).
    """
    header = request.headers.get("authorization")
    if header and header.lower().startswith("bearer "):
        return f"token:{hash(header[7:])}"
    name = settings.client_ip_header
    if name:
        viewer = viewer_address(request.headers.get(name, ""))
        if viewer is not None:
            return f"ip:{viewer}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


class RateLimit:
    """Dependency factory: `Depends(RateLimit(5, 60))`."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limiter = SlidingWindowLimiter(limit, window_seconds)

    async def __call__(self, request: Request) -> None:
        allowed, retry_after = self.limiter.check(client_key(request))
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Try again shortly.",
                headers={"Retry-After": str(int(retry_after) + 1)},
            )


# Shared instances -- one limiter per protected surface, so a burst of logins
# cannot consume the budget for starting sessions.
login_limit = RateLimit(limit=10, window_seconds=60)
register_limit = RateLimit(limit=5, window_seconds=300)
session_create_limit = RateLimit(limit=20, window_seconds=60)
stream_limit = RateLimit(limit=30, window_seconds=60)
