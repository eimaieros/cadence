"""Probes and request correlation.

The probe tests are the ones that matter. Getting liveness and readiness the
wrong way round is not a subtle bug — it turns a database blip into a restart
loop across every replica, at the exact moment the database is least able to
cope with a stampede of reconnecting clients.
"""

from __future__ import annotations

import json
import logging

import pytest

from app.observability import (
    HEADER,
    JsonFormatter,
    RequestIdFilter,
    clean_incoming,
    request_id_var,
)


# ── the two probes answer different questions ────────────────────────────────


@pytest.mark.asyncio
async def test_liveness_touches_nothing(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # If a key named after a dependency ever appears here, someone has put a
    # dependency check back into the liveness probe.
    assert "database" not in body


@pytest.mark.asyncio
async def test_readiness_reports_the_database(client):
    r = await client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["database"] == "up"


@pytest.mark.asyncio
async def test_readiness_fails_closed_when_the_database_is_gone(client, monkeypatch):
    """503, so the load balancer stops sending traffic — and no restart.

    This is the whole point of the split. The process is healthy; the thing it
    depends on is not. Killing the process would drop its connection pool and
    its in-flight streams and make the recovery slower.
    """
    from app import main

    class DeadEngine:
        def connect(self):
            raise OSError("connection refused")

    monkeypatch.setattr(main, "engine", DeadEngine())

    r = await client.get("/ready")
    assert r.status_code == 503
    assert r.json()["database"] == "down"

    # And liveness is still fine, which is the assertion that stops a future
    # refactor from quietly re-merging the two.
    assert (await client.get("/health")).status_code == 200


# ── request ids ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_response_carries_an_id(client):
    r = await client.get("/health")
    assert len(r.headers.get(HEADER, "")) >= 8


@pytest.mark.asyncio
async def test_two_requests_get_two_ids(client):
    a = (await client.get("/health")).headers[HEADER]
    b = (await client.get("/health")).headers[HEADER]
    assert a != b


@pytest.mark.asyncio
async def test_a_caller_supplied_id_is_reused(client):
    r = await client.get("/health", headers={HEADER: "trace-abc-123"})
    assert r.headers[HEADER] == "trace-abc-123"


@pytest.mark.asyncio
async def test_a_hostile_id_is_replaced_not_echoed(client):
    """Log injection: a newline in the header would forge a whole log line.

    The header is attacker-controlled and ends up in the logs, so it is
    filtered rather than trusted. A request whose id we refuse still gets one —
    it just does not get theirs.
    """
    forjado = "abc\nWARNING root: transfer approved"
    r = await client.get("/health", headers={HEADER: forjado})
    assert r.headers[HEADER] != forjado
    assert "\n" not in r.headers[HEADER]


@pytest.mark.parametrize(
    "cru",
    [
        None,
        "",
        "   ",
        "a" * 65,                    # unbounded length is a log-size problem
        "abc def",                   # space
        "abc\rdef",                  # carriage return
        "abc\ndef",                  # newline
        "<script>alert(1)</script>", # ends up in a log viewer that renders HTML
    ],
)
def test_ids_we_refuse(cru):
    assert clean_incoming(cru) is None


@pytest.mark.parametrize("cru", ["abc123", "trace-1.2.3", "a_b:c", "A" * 64])
def test_ids_we_accept(cru):
    assert clean_incoming(cru) == cru


# ── the log format ───────────────────────────────────────────────────────────


def test_a_log_line_is_one_json_object_carrying_the_id():
    token = request_id_var.set("deadbeef")
    try:
        record = logging.LogRecord(
            "cadence", logging.INFO, __file__, 1, "hello", None, None
        )
        RequestIdFilter().filter(record)
        linha = JsonFormatter().format(record)
    finally:
        request_id_var.reset(token)

    obj = json.loads(linha)
    assert obj["request_id"] == "deadbeef"
    assert obj["message"] == "hello"
    assert obj["level"] == "INFO"
    assert "\n" not in linha, "one object per line, or nothing can parse it"


def test_an_exception_goes_in_the_object_not_across_ten_lines():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "cadence", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )
    RequestIdFilter().filter(record)
    obj = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in obj["exception"]
