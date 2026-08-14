"""Application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.db import engine
from app.llm.client import build_provider
from app.routers import auth, sessions

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
    """Liveness plus a real dependency check.

    A health endpoint that returns 200 while the database is unreachable is
    worse than none -- it tells the orchestrator everything is fine while every
    request fails.
    """
    db_ok = True
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        logger.exception("health check: database unreachable")
        db_ok = False

    body = {
        "status": "ok" if db_ok else "degraded",
        "database": "up" if db_ok else "down",
        "llm_provider": getattr(app.state, "llm", None).name if hasattr(app.state, "llm") else "unknown",
        "version": app.version,
    }
    return JSONResponse(
        status_code=status.HTTP_200_OK if db_ok else status.HTTP_503_SERVICE_UNAVAILABLE,
        content=body,
    )


app.include_router(auth.router)
app.include_router(sessions.router)
