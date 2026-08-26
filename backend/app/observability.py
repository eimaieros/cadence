"""Request correlation and structured logs.

WHY THIS EXISTS AT ALL, IN AN APP THIS SIZE.

Because of the streaming. A normal request produces one log line and you can
find it by timestamp. A streamed answer produces a scatter of lines — the
request arrives, the provider is called, tokens flow for eight seconds, the turn
is persisted by a *different* database session than the one the request opened,
and the client may abort halfway through. Under any concurrency at all those
lines interleave with somebody else's, and "find the request that failed" turns
into reading timestamps and guessing.

An id per request makes that a filter instead of a guess.

WHY THE HEADER IS TRUSTED, AND WHAT THAT COSTS.

If a caller sends `X-Request-ID`, that value is reused so a trace can span more
than one service. That means accepting a string from outside and putting it in
the logs, which is a log-injection risk: a newline in the header lets an
attacker forge log lines. So the value is bounded and filtered down to
characters that cannot break a line — anything else and we mint our own.
"""

from __future__ import annotations

import contextvars
import json
import logging
import time
import uuid
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

HEADER = "X-Request-ID"

# A ContextVar and not a plain global: with async handlers there are many
# requests in flight on one thread, and a module-level variable would hand
# every log line whichever id was set most recently.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)

# Printable ASCII minus the two characters that can forge a log line.
_SAFE = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:"
)
_MAX_LEN = 64


def clean_incoming(raw: str | None) -> str | None:
    """Return a caller-supplied id if it is safe to log, else None.

    Rejecting is the safe default: a request whose id we refuse still gets one,
    it just does not get *theirs*.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw or len(raw) > _MAX_LEN:
        return None
    if not all(c in _SAFE for c in raw):
        return None
    return raw


class RequestIdFilter(logging.Filter):
    """Puts the current request id on every record, including library ones."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line.

    Plain text is easier to read over your own shoulder; JSON is the only thing
    a log aggregator can filter on without a fragile regex. The `debug` setting
    picks between them, so local work stays readable.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        for key in ("method", "path", "status", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign an id, log the request, return the id in the response."""

    def __init__(self, app, logger_name: str = "cadence.access") -> None:
        super().__init__(app)
        self.log = logging.getLogger(logger_name)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        rid = clean_incoming(request.headers.get(HEADER)) or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        request.state.request_id = rid

        # perf_counter, not time.time: the wall clock can step backwards over an
        # NTP correction and produce a negative duration, which then poisons any
        # average built on top of it.
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = (time.perf_counter() - started) * 1000
            self.log.exception(
                "unhandled",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": 500,
                    "duration_ms": round(duration, 1),
                },
            )
            raise
        finally:
            request_id_var.reset(token)

        duration = (time.perf_counter() - started) * 1000
        response.headers[HEADER] = rid

        # Health probes run every few seconds forever. Logging them at INFO
        # buries everything else and costs money in any hosted log product.
        level = logging.DEBUG if request.url.path in ("/health", "/ready") else logging.INFO
        self.log.log(
            level,
            "%s %s -> %s",
            request.method,
            request.url.path,
            response.status_code,
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(duration, 1),
            },
        )
        return response


def configure_logging(*, debug: bool) -> None:
    """Install the filter and formatter on the root handler.

    Root, not `cadence` — so that SQLAlchemy, uvicorn and anything else that
    logs during a request carries the same id. A correlation id that only
    covers your own log calls is not a correlation id.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    for existing in list(root.handlers):
        root.removeHandler(existing)

    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    if debug:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s"
            )
        )
    else:
        handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
