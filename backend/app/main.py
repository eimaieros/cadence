"""Application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.db import database_reachable, engine
from app.llm.client import build_provider
from app.observability import RequestIdMiddleware, configure_logging
from app.routers import auth, sessions

configure_logging(debug=settings.debug)
logger = logging.getLogger("cadence")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own expensive, long-lived resources here rather than per request.

    The HTTP client to the model API keeps a connection pool. Building one per
    request means a fresh TLS handshake every time, which is pure latency.
    """
    provider = build_provider()
    app.state.llm = provider
    logger.info(
        "started env=%s llm_provider=%s cost_ceiling=$%.2f",
        settings.environment,
        provider.name,
        settings.session_cost_ceiling_usd,
    )
    if not settings.llm_enabled:
        logger.warning(
            "ANTHROPIC_API_KEY is not set -- running the scripted interviewer. "
            "Everything works; the questions are fixed."
        )
    try:
        yield
    finally:
        close = getattr(provider, "aclose", None)
        if close is not None:
            await close()
        await engine.dispose()
        logger.info("shutdown complete")


app = FastAPI(
    title="Cadence API",
    description=(
        "Practice technical interviews with an interviewer that asks follow-up "
        "questions, then returns an evidence-based scorecard."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# Added last, so it runs first: the id has to exist before anything else logs.
app.add_middleware(RequestIdMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Without this the browser can see the header exists but not read it, so a
    # user reporting a bug cannot tell you which request it was.
    expose_headers=["X-Request-ID"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Log the detail, return none of it.

    Stack traces and driver errors in a response body are reconnaissance.
    """
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Something went wrong on our side."},
    )


@app.get("/health", tags=["ops"])
async def health() -> dict:
    """Liveness. Deliberately checks nothing.

    THIS USED TO CHECK THE DATABASE, AND THAT WAS A BUG WAITING FOR AN OUTAGE.

    Liveness and readiness answer different questions and the orchestrator does
    different things with the answers:

      liveness  fails -> restart the container
      readiness fails -> stop sending it traffic, leave it running

    A liveness probe that touches the database turns a database blip into a
    restart loop across every replica at once. The processes were fine; the
    only thing wrong with them was that something else was down. Restarting
    them drops their connection pools and their in-flight streams and makes the
    recovery slower, right when the database is already struggling.

    So this endpoint answers exactly one question — can this process still
    serve a request — and the answer is yes, because it just did.
    """
    return {"status": "ok", "version": app.version}


@app.get("/ready", tags=["ops"])
async def ready() -> JSONResponse:
    """Readiness. Can this instance usefully serve traffic right now?

    Fails with 503 when the database is unreachable, which takes this replica
    out of the load balancer without killing it. When the database comes back,
    the next probe succeeds and traffic returns, with no restart in between.
    """
    # Through SessionFactory, not the engine — see app/db.py. Checking a
    # different connection path than the one requests take is how you get a
    # probe that reports ready while every request fails.
    db_ok = await database_reachable()
    if not db_ok:
        logger.warning("readiness: database unreachable")

    provider = getattr(app.state, "llm", None)
    body = {
        "status": "ready" if db_ok else "not ready",
        "database": "up" if db_ok else "down",
        "llm_provider": provider.name if provider is not None else "unknown",
        "version": app.version,
    }
    return JSONResponse(
        status_code=status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body,
    )


app.include_router(auth.router)
app.include_router(sessions.router)
