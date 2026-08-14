"""LLM providers.

Two implementations behind one protocol:

* `AnthropicProvider` -- real streaming calls over httpx, with exponential
  backoff plus jitter, and per-call token accounting.
* `ScriptedProvider` -- no network, no credentials. Used by the test suite and
  whenever no API key is configured, so `docker compose up` gives a working
  product on a laptop with nothing to sign up for.

The provider is chosen once at startup and injected, which is what makes the
tests deterministic: they assert on transport and persistence behaviour, not on
what a model happened to say that afternoon.

Cost note: prices are hardcoded estimates for accounting only. They exist so
the ceiling can be enforced, not to produce an invoice. Confirm current prices
at https://docs.claude.com/en/docs/about-claude/pricing before relying on them.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from app.config import settings

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

# (input $/Mtok, output $/Mtok)
PRICES: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-opus-5": (5.0, 25.0),
}
_FALLBACK_PRICE = (3.0, 15.0)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    in_price, out_price = PRICES.get(model, _FALLBACK_PRICE)
    return (input_tokens / 1_000_000) * in_price + (output_tokens / 1_000_000) * out_price


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    @property
    def cost_usd(self) -> float:
        return estimate_cost(self.model, self.input_tokens, self.output_tokens)


@dataclass
class StreamResult:
    """Filled in as a stream is consumed; read after it completes."""

    text: str = ""
    usage: Usage = field(default_factory=Usage)


class LLMError(Exception):
    """Upstream model failure that survived retries."""


class Provider(Protocol):
    name: str

    def stream(
        self, *, system: str, messages: list[dict], model: str, result: StreamResult
    ) -> AsyncIterator[str]: ...

    async def complete(self, *, system: str, messages: list[dict], model: str) -> tuple[str, Usage]: ...


# --------------------------------------------------------------------------
# Real provider
# --------------------------------------------------------------------------


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, timeout: float = 60.0, max_attempts: int = 4) -> None:
        self._api_key = api_key
        self._max_attempts = max_attempts
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=10.0),
            headers={
                "x-api-key": api_key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential with full jitter.

        Without jitter, every client that failed at the same moment retries at
        the same moment, and the recovering upstream gets knocked over again by
        a synchronised thundering herd.
        """
        return random.uniform(0, min(2**attempt, 16))

    async def stream(
        self, *, system: str, messages: list[dict], model: str, result: StreamResult
    ) -> AsyncIterator[str]:
        payload = {
            "model": model,
            "max_tokens": settings.llm_max_tokens,
            "system": system,
            "messages": messages,
            "stream": True,
        }
        last_error: Exception | None = None

        for attempt in range(self._max_attempts):
            chunks: list[str] = []
            usage = Usage(model=model)
            try:
                async with self._client.stream("POST", API_URL, json=payload) as response:
                    if response.status_code in (429, 500, 502, 503, 529):
                        await response.aread()
                        raise httpx.HTTPStatusError(
                            f"retryable upstream status {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw:
                            continue
                        event = json.loads(raw)
                        etype = event.get("type")

                        if etype == "content_block_delta":
                            piece = event.get("delta", {}).get("text", "")
                            if piece:
                                chunks.append(piece)
                                yield piece
                        elif etype == "message_start":
                            u = event.get("message", {}).get("usage", {})
                            usage.input_tokens = u.get("input_tokens", 0)
                        elif etype == "message_delta":
                            u = event.get("usage", {})
                            usage.output_tokens = u.get("output_tokens", 0)
                        elif etype == "error":
                            raise LLMError(str(event.get("error", "upstream error")))

                result.text = "".join(chunks)
                result.usage = usage
                return
            except (httpx.HTTPStatusError, httpx.TransportError, json.JSONDecodeError) as exc:
                last_error = exc
                # Only retry if nothing was emitted yet. Once bytes have reached
                # the client, restarting would duplicate visible output --
                # a partial answer is better than a doubled one.
                if chunks:
                    result.text = "".join(chunks)
                    result.usage = usage
                    return
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(self._backoff(attempt))
                    continue

        raise LLMError(f"model stream failed after {self._max_attempts} attempts: {last_error}")

    async def complete(self, *, system: str, messages: list[dict], model: str) -> tuple[str, Usage]:
        payload = {
            "model": model,
            "max_tokens": settings.llm_max_tokens,
            "system": system,
            "messages": messages,
        }
        last_error: Exception | None = None

        for attempt in range(self._max_attempts):
            try:
                response = await self._client.post(API_URL, json=payload)
                if response.status_code in (429, 500, 502, 503, 529):
                    raise httpx.HTTPStatusError(
                        f"retryable upstream status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                body = response.json()
                text = "".join(
                    block.get("text", "") for block in body.get("content", []) if block.get("type") == "text"
                )
                u = body.get("usage", {})
                return text, Usage(
                    input_tokens=u.get("input_tokens", 0),
                    output_tokens=u.get("output_tokens", 0),
                    model=model,
                )
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self._max_attempts - 1:
                    await asyncio.sleep(self._backoff(attempt))

        raise LLMError(f"model call failed after {self._max_attempts} attempts: {last_error}")


# --------------------------------------------------------------------------
# Offline provider
# --------------------------------------------------------------------------

_QUESTION_BANK = [
    "Walk me through a system you've shipped that had real users on it. What was "
    "the hardest constraint you were working under?",
    "You mentioned that setup -- where would it break first if traffic went up "
    "ten times overnight, and how would you know?",
    "Tell me about a bug that took you far too long to find. What was the actual "
    "cause, and what did you change afterwards so it couldn't happen again?",
    "How do you decide when a piece of code is worth abstracting versus leaving "
    "duplicated? Give me a case where you got that call wrong.",
    "Describe a time you disagreed with someone on a technical decision. What did "
    "you do, and what happened?",
    "Last one: what's something in your current stack you think is a mistake, and "
    "what would you replace it with?",
]

_SCRIPTED_SCORE = {
    "overall": 68,
    "summary": (
        "You explain what you built clearly and you're comfortable talking about "
        "production constraints. Where you lose ground is specificity -- several "
        "answers described an approach without the numbers or outcome that would "
        "let an interviewer judge the scale you worked at."
    ),
    "dimensions": [
        {"name": "Technical depth", "score": 4, "note": "Comfortable below the framework layer, especially on failure modes."},
        {"name": "Structure", "score": 4, "note": "Answers had a clear beginning and end without rambling."},
        {"name": "Specificity", "score": 2, "note": "Claims about scale and impact mostly arrived without numbers attached."},
        {"name": "Trade-off reasoning", "score": 3, "note": "Named alternatives, but rarely said what each option cost."},
        {"name": "Communication", "score": 4, "note": "Plain language, and you corrected yourself out loud rather than bluffing."},
    ],
    "strengths": [
        "You described the failure mode before the happy path, which reads as production experience.",
        "You corrected an answer mid-sentence instead of defending it.",
    ],
    "gaps": [
        "Attach a number to every claim about scale -- users, requests, latency, or money.",
        "When you name an alternative approach, say what choosing it would have cost.",
        "Close each answer with the outcome. Several stopped at the method.",
    ],
}


class ScriptedProvider:
    """Deterministic provider used offline and in tests.

    Streams token by token with a small delay so the front end exercises the
    real streaming path rather than a special case -- an SSE bug that only
    appears against the real API is a bug you find in production.
    """

    name = "scripted"

    def __init__(self, delay: float = 0.02) -> None:
        self._delay = delay

    @staticmethod
    def _pick_question(messages: list[dict]) -> str:
        asked = sum(1 for m in messages if m.get("role") == "assistant")
        if asked >= len(_QUESTION_BANK):
            return "That's everything I wanted to cover. Thanks -- end the session to see your scorecard."
        return _QUESTION_BANK[asked]

    async def stream(
        self, *, system: str, messages: list[dict], model: str, result: StreamResult
    ) -> AsyncIterator[str]:
        text = self._pick_question(messages)
        emitted: list[str] = []
        for word in text.split(" "):
            piece = word + " "
            emitted.append(piece)
            yield piece
            if self._delay:
                await asyncio.sleep(self._delay)
        final = "".join(emitted).strip()
        result.text = final
        # Rough token estimate purely so the cost path is exercised end to end.
        result.usage = Usage(
            input_tokens=len(system) // 4,
            output_tokens=max(1, len(final) // 4),
            model=model,
        )

    async def complete(self, *, system: str, messages: list[dict], model: str) -> tuple[str, Usage]:
        payload = json.dumps(_SCRIPTED_SCORE)
        return payload, Usage(
            input_tokens=len(system) // 4, output_tokens=len(payload) // 4, model=model
        )


def build_provider() -> Provider:
    if settings.llm_enabled and settings.anthropic_api_key:
        return AnthropicProvider(settings.anthropic_api_key)
    return ScriptedProvider()
