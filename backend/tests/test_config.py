"""Configuration fails at deployment, before it can fail on a request."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_production_jwt_secret_needs_32_utf8_bytes():
    with pytest.raises(ValidationError):
        Settings(environment="production", jwt_secret="short", _env_file=None)
    configured = Settings(environment="production", jwt_secret="x" * 32, _env_file=None)
    assert len(configured.jwt_secret) == 32


@pytest.mark.parametrize(
    "field,value",
    [
        ("db_pool_size", 0),
        ("db_max_overflow", -1),
        ("access_token_ttl_minutes", 0),
        ("refresh_token_ttl_days", 0),
        ("llm_max_tokens", 0),
        ("session_cost_ceiling_usd", float("nan")),
    ],
)
def test_operational_numbers_are_bounded(field, value):
    with pytest.raises(ValidationError):
        Settings(**{field: value}, _env_file=None)


def test_only_the_implemented_jwt_algorithm_is_accepted():
    with pytest.raises(ValidationError):
        Settings(jwt_algorithm="none", _env_file=None)
