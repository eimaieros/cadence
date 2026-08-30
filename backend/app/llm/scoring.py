"""Turning a transcript into a validated scorecard.

The rule this file exists to enforce: **never regex free text out of a model.**
The model is asked for JSON, the JSON is parsed, and the result is validated
against a Pydantic model with bounded score ranges. If validation fails, that is
a retry -- not a shrug, and not a best-effort scrape of whatever came back.

If it still fails after the retries, the caller gets an explicit failure. A
scorecard invented from a malformed response is worse than no scorecard,
because the user cannot tell the difference.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.config import settings
from app.llm.client import LLMError, Provider, Usage
from app.llm.prompts import render_transcript, scorer_system
from app.models import Turn
from app.schemas import DimensionScore

logger = logging.getLogger(__name__)

EXPECTED_DIMENSIONS = {
    "Technical depth",
    "Structure",
    "Specificity",
    "Trade-off reasoning",
    "Communication",
}


class ScoreDraft(BaseModel):
    """What the model is asked to produce, with the bounds enforced in code.

    A prompt saying "score 1-5" is a request. This is the guarantee.
    """

    overall: int = Field(ge=0, le=100)
    summary: str = Field(min_length=1, max_length=1200)
    dimensions: list[DimensionScore] = Field(
        min_length=len(EXPECTED_DIMENSIONS), max_length=len(EXPECTED_DIMENSIONS)
    )
    strengths: list[str] = Field(default_factory=list, max_length=8)
    gaps: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("dimensions")
    @classmethod
    def _known_dimensions(cls, v: list[DimensionScore]) -> list[DimensionScore]:
        names = {d.name for d in v}
        if names != EXPECTED_DIMENSIONS:
            missing = EXPECTED_DIMENSIONS - names
            unknown = names - EXPECTED_DIMENSIONS
            raise ValueError(
                f"dimensions must match the rubric exactly; "
                f"missing={sorted(missing)}, unexpected={sorted(unknown)}"
            )
        return v


def _extract_json(raw: str) -> str:
    """Salvage a JSON object from a response that may carry stray prose.

    Not a parser -- it finds the outermost braces and lets json.loads be the
    judge. If the model wrapped the object in a markdown fence or a sentence,
    this recovers it; if it produced something genuinely broken, this fails and
    the retry path handles it.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found in response")
    return cleaned[start : end + 1]


class ScoringError(Exception):
    """Scoring failed in a way the caller must surface, not paper over."""


async def score_transcript(
    provider: Provider, turns: list[Turn], *, max_attempts: int = 3
) -> tuple[ScoreDraft, Usage]:
    transcript = render_transcript(turns)
    system = scorer_system()
    messages = [
        {
            "role": "user",
            "content": (
                "Assess this practice interview transcript and return the JSON "
                f"scorecard described in your instructions.\n\n{transcript}"
            ),
        }
    ]

    last_problem: str | None = None
    total = Usage(model=settings.llm_model_scorer)

    for attempt in range(max_attempts):
        try:
            raw, usage = await provider.complete(
                system=system, messages=messages, model=settings.llm_model_scorer
            )
        except LLMError as exc:
            raise ScoringError(f"scoring call failed: {exc}") from exc

        total.input_tokens += usage.input_tokens
        total.output_tokens += usage.output_tokens

        try:
            draft = ScoreDraft.model_validate(json.loads(_extract_json(raw)))
        except (ValueError, ValidationError) as exc:
            last_problem = str(exc)
            logger.warning("scorecard validation failed (attempt %s): %s", attempt + 1, last_problem)
            # Feed the failure back so the retry is informed rather than a
            # coin flip on the same prompt.
            messages = messages[:1] + [
                {"role": "assistant", "content": raw[:2000]},
                {
                    "role": "user",
                    "content": (
                        "That response did not validate against the required schema. "
                        f"Problem: {last_problem}. Return ONLY the corrected JSON object."
                    ),
                },
            ]
            continue

        return draft, total

    raise ScoringError(f"scorecard did not validate after {max_attempts} attempts: {last_problem}")
