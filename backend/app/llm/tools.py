"""Tools the interviewer can call while it decides what to ask next.

Three of them, and the shape of each is a decision:

* ``get_coverage`` -- which focus areas the transcript has touched and how many
  questions are left. The model is bad at counting its own turns; the database
  is not.
* ``get_rubric`` -- what a 1, a 3 and a 5 look like on one of the five scored
  dimensions, so a follow-up can aim at the dimension an answer was weakest on.
  The dimensions are exactly the scorer's -- a test holds them together -- so
  the interview probes what will be scored.
* ``flag_follow_up`` -- record that an answer left something to come back to.
  The record is the audit row itself; nothing else is written.

What the model is never given:

* **Identifiers.** Every handler is bound to the session that is streaming, on
  the server. No tool takes a session id, so no prompt -- injected or not --
  can point one at someone else's interview. Inputs forbid unknown fields, so
  adding ``"session_id"`` to a call is a validation error, not an ignored key.
* **Free rein.** Tools come from a fixed allowlist, inputs are validated with
  Pydantic before a handler runs, and a failed call becomes an error *result*
  the model can read, never an exception that ends the stream.

Tool results go back to the model as data. They are computed by the application
from the session -- but ``flag_follow_up`` echoes a topic the model wrote after
reading candidate text, so results are treated with the same suspicion as an
answer: never interpolated into a system prompt, never shown to the scorer.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

logger = logging.getLogger(__name__)

# Rounds of tool use allowed before the question has to be written. The last
# round offers no tools at all, so a model that keeps asking for them still
# produces a question instead of looping on the interview's budget.
MAX_TOOL_ROUNDS = 2
# Calls the model may make in one round. Extra calls are answered with an error
# result rather than executed: every tool_use needs a tool_result, so they
# cannot simply be dropped.
MAX_CALLS_PER_ROUND = 3

DIMENSIONS = (
    "Technical depth",
    "Structure",
    "Specificity",
    "Trade-off reasoning",
    "Communication",
)

Dimension = Literal[
    "Technical depth",
    "Structure",
    "Specificity",
    "Trade-off reasoning",
    "Communication",
]

RUBRIC: dict[str, dict[str, str]] = {
    "Technical depth": {
        "1": "Names technologies but cannot say what happens underneath them.",
        "3": "Explains how the parts work on the happy path; failure modes are vague.",
        "5": "Goes below the framework layer and starts from how it fails.",
    },
    "Structure": {
        "1": "Answers wander and end without a point.",
        "3": "Has a beginning and an end, but the middle drifts.",
        "5": "Context, decision, result -- in that order, without being asked.",
    },
    "Specificity": {
        "1": "Claims about scale or impact arrive with no numbers, names or outcomes.",
        "3": "Some numbers, not tied to what they changed.",
        "5": "Every claim carries a number, a name and the outcome it produced.",
    },
    "Trade-off reasoning": {
        "1": "One option presented as the only option.",
        "3": "Names alternatives without saying what each would have cost.",
        "5": "Says what was given up, why, and when the other choice would win.",
    },
    "Communication": {
        "1": "Jargon instead of explanation; hard to follow.",
        "3": "Clear, but leans on the listener to fill gaps.",
        "5": "Plain language, and corrects itself out loud rather than bluffing.",
    },
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CoverageInput(_Strict):
    """No arguments: the session is implied, on purpose."""


class RubricInput(_Strict):
    dimension: Dimension


class FollowUpInput(_Strict):
    topic: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    reason: Literal["no_number", "vague_claim", "unexplained_tradeoff", "contradiction"]


@dataclass(frozen=True)
class ToolContext:
    """Everything a handler may read, fixed by the server before the model runs."""

    focus_areas: list[str]
    transcript: list[str]
    questions_asked: int
    max_questions: int


@dataclass
class ToolOutcome:
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)


async def _coverage(_args: CoverageInput, ctx: ToolContext) -> dict[str, Any]:
    text = "\n".join(ctx.transcript).casefold()
    return {
        "questions_asked": ctx.questions_asked,
        "questions_remaining": max(0, ctx.max_questions - ctx.questions_asked),
        "focus_areas": [
            {"area": area, "mentioned": area.casefold() in text} for area in ctx.focus_areas
        ],
    }


async def _rubric(args: RubricInput, _ctx: ToolContext) -> dict[str, Any]:
    return {"dimension": args.dimension, "levels": RUBRIC[args.dimension]}


async def _follow_up(args: FollowUpInput, _ctx: ToolContext) -> dict[str, Any]:
    return {"recorded": True, "topic": args.topic, "reason": args.reason}


Handler = Callable[[Any, ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
    handler: Handler

    def spec(self) -> dict[str, Any]:
        """The definition sent to the model API."""
        schema = self.input_model.model_json_schema()
        schema.pop("title", None)
        schema.pop("description", None)
        return {"name": self.name, "description": self.description, "input_schema": schema}


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in (
        Tool(
            "get_coverage",
            "How many questions have been asked and remain, and which of the "
            "candidate's focus areas the conversation has touched so far.",
            CoverageInput,
            _coverage,
        ),
        Tool(
            "get_rubric",
            "What a 1, a 3 and a 5 look like on one scored dimension, to aim a "
            "follow-up at the dimension an answer was weakest on.",
            RubricInput,
            _rubric,
        ),
        Tool(
            "flag_follow_up",
            "Record that the last answer left something to come back to: a claim "
            "without a number, a vague claim, a trade-off with no cost attached, "
            "or a contradiction. Then ask about it.",
            FollowUpInput,
            _follow_up,
        ),
    )
}


def tool_specs() -> list[dict[str, Any]]:
    return [t.spec() for t in TOOLS.values()]


async def run_tool(name: str, raw: Any, ctx: ToolContext) -> ToolOutcome:
    """Validate and execute one call. Never raises: failures become results."""
    tool = TOOLS.get(name)
    if tool is None:
        return ToolOutcome(False, {"error": f"unknown tool: {name[:64]}"})
    if not isinstance(raw, dict):
        return ToolOutcome(False, {"error": "tool input must be a JSON object"})
    try:
        args = tool.input_model.model_validate(raw)
    except ValidationError as exc:
        problems = [
            {"field": ".".join(str(p) for p in e["loc"]) or "(root)", "problem": e["msg"]}
            for e in exc.errors(include_url=False, include_input=False)
        ]
        return ToolOutcome(False, {"error": "invalid input", "problems": problems})
    try:
        return ToolOutcome(True, await tool.handler(args, ctx))
    except Exception:
        logger.exception("tool %s failed", name)
        return ToolOutcome(False, {"error": "the tool failed"})
