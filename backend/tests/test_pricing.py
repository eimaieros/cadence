"""The local spend estimate follows the configured model catalogue."""

import pytest

from app.llm.client import estimate_cost


def test_sonnet_5_uses_the_current_standard_rate():
    assert estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000) == pytest.approx(12.0)


def test_an_unknown_model_uses_a_conservative_fallback():
    assert estimate_cost("future-unpriced-model", 1_000_000, 1_000_000) == pytest.approx(30.0)
