"""Provider retry policy at the HTTP boundary."""

from __future__ import annotations

import httpx
import pytest

from app.llm.client import API_URL, AnthropicProvider, LLMError

pytestmark = pytest.mark.asyncio


class StubClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.calls = 0

    async def post(self, *_args, **_kwargs) -> httpx.Response:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


def response(status: int, **kwargs) -> httpx.Response:
    return httpx.Response(
        status,
        request=httpx.Request("POST", API_URL),
        **kwargs,
    )


def provider_with(client: StubClient, attempts: int = 4) -> AnthropicProvider:
    provider = object.__new__(AnthropicProvider)
    provider._max_attempts = attempts
    provider._client = client
    provider._backoff = lambda _attempt: 0
    return provider


async def test_non_retryable_auth_failure_is_attempted_once():
    client = StubClient([response(401, json={"error": "invalid key"})])
    provider = provider_with(client)

    with pytest.raises(LLMError):
        await provider.complete(system="x", messages=[], model="test-model")
    assert client.calls == 1, "a bad key will not become valid after backoff"


async def test_rate_limit_is_retried_before_any_output():
    client = StubClient([
        response(429, json={"error": "slow down"}),
        response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 2, "output_tokens": 1},
            },
        ),
    ])
    provider = provider_with(client, attempts=2)

    text, usage = await provider.complete(system="x", messages=[], model="test-model")
    assert text == "ok"
    assert usage.input_tokens == 2
    assert client.calls == 2


async def test_invalid_success_bodies_become_provider_errors():
    bad_responses = [
        response(200, content=b"not json"),
        response(200, json=[]),
        response(200, json={"content": ["not-a-block"], "usage": {}}),
        response(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": -1, "output_tokens": 1},
            },
        ),
    ]

    for bad_response in bad_responses:
        provider = provider_with(StubClient([bad_response]))
        with pytest.raises(LLMError):
            await provider.complete(system="x", messages=[], model="test-model")
