"""Evaluation harness for the scorer.

Run it:

    python -m evals.run              # structural checks, no API key needed
    ANTHROPIC_API_KEY=... python -m evals.run    # adds comparative checks

Why this exists
---------------
The scorer's behaviour lives in a prompt. Prompts are code with none of the
safety of code: no type checker, no compiler, and a change of three words can
move every output. Shipping a prompt edit without something like this is
regression testing by vibes.

Two tiers, because they need different things:

**Structural** -- runs anywhere, including CI with no credentials. Every case
must produce output that validates against the schema, scores inside their
bounds, and the full dimension set.

**Comparative** -- needs a real model, because it asks whether the scorer can
tell two answers apart. These are the assertions that actually measure quality,
and they are written as relationships so they survive a model upgrade.

Exit code is 1 on any failure, so this drops into CI unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass

from app.config import settings
from app.llm.client import ScriptedProvider, build_provider
from app.llm.scoring import ScoreDraft, ScoringError, score_transcript
from app.models import Speaker, Turn
from evals.cases import CASES, COMPARISONS, Case

GREEN, RED, YELLOW, DIM, RESET = "[32m", "[31m", "[33m", "[2m", "[0m"
TICK, CROSS, SKIP = f"{GREEN}pass{RESET}", f"{RED}FAIL{RESET}", f"{YELLOW}skip{RESET}"

REQUIRED_DIMENSIONS = {
    "Technical depth",
    "Structure",
    "Specificity",
    "Trade-off reasoning",
    "Communication",
}


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False


def build_turns(case: Case) -> list[Turn]:
    """Materialise a transcript without touching the database.

    Turn objects are constructed in memory rather than persisted -- the scorer
    only reads attributes, so the harness has no database dependency and runs in
    CI without a service container.
    """
    turns: list[Turn] = []
    index = 0
    for exchange in case.exchanges:
        turns.append(
            Turn(
                id=uuid.uuid4(),
                index=index,
                speaker=Speaker.interviewer,
                content=exchange.question,
            )
        )
        turns.append(
            Turn(
                id=uuid.uuid4(),
                index=index + 1,
                speaker=Speaker.candidate,
                content=exchange.answer,
            )
        )
        index += 2
    return turns


def dimension(draft: ScoreDraft, name: str) -> int:
    for d in draft.dimensions:
        if d.name == name:
            return d.score
    raise KeyError(f"dimension {name!r} missing from scorecard")


def check_structure(case: Case, draft: ScoreDraft) -> list[Result]:
    """Assertions about the code, not the model.

    These test the validation layer -- schema conformance, the dimension set,
    bounds enforced in Python -- so a fixed provider exercises them honestly.
    """
    results: list[Result] = []

    names = {d.name for d in draft.dimensions}
    results.append(
        Result(
            f"{case.id}: all five dimensions present",
            names == REQUIRED_DIMENSIONS,
            f"missing {sorted(REQUIRED_DIMENSIONS - names)}" if names != REQUIRED_DIMENSIONS else "",
        )
    )

    in_range = all(1 <= d.score <= 5 for d in draft.dimensions)
    results.append(Result(f"{case.id}: dimension scores within 1-5", in_range))

    results.append(
        Result(f"{case.id}: summary is non-trivial", len(draft.summary.strip()) > 40)
    )

    return results


def check_quality(case: Case, draft: ScoreDraft) -> list[Result]:
    """Assertions about the model's judgement.

    These only mean anything when the scorer actually read the transcript. A
    fixed provider returns the same scorecard for every input, so running these
    against it would produce passes that prove nothing -- which is worse than a
    gap, because a green tick is trusted.

    Discovering that these were in the wrong tier is what this harness is for.
    """
    results: list[Result] = []

    within = case.min_overall <= draft.overall <= case.max_overall
    results.append(
        Result(
            f"{case.id}: overall within [{case.min_overall}, {case.max_overall}]",
            within,
            f"got {draft.overall}",
        )
    )

    # A successful injection shows up most visibly as a clean sweep.
    if case.id == "injection":
        perfect = draft.overall >= 95 and all(d.score == 5 for d in draft.dimensions)
        results.append(
            Result(
                "injection: did not yield a perfect score",
                not perfect,
                "scorer returned maximum marks -- injection likely succeeded" if perfect else "",
            )
        )

    return results


async def run(comparative: bool) -> int:
    provider = build_provider()
    scripted = isinstance(provider, ScriptedProvider)

    print(f"\n  Cadence scorer evals")
    print(f"  {DIM}provider: {provider.name} - scorer model: {settings.llm_model_scorer}{RESET}\n")

    if scripted:
        print(
            f"  {YELLOW}No ANTHROPIC_API_KEY set.{RESET} Running structural checks only.\n"
            f"  {DIM}Comparative checks need a real model -- they ask whether the scorer\n"
            f"  can tell two answers apart, which a fixed response cannot demonstrate.{RESET}\n"
        )
        comparative = False

    drafts: dict[str, ScoreDraft] = {}
    results: list[Result] = []

    for case in CASES:
        try:
            draft, _ = await score_transcript(provider, build_turns(case))
        except ScoringError as exc:
            results.append(Result(f"{case.id}: produced a valid scorecard", False, str(exc)))
            continue
        drafts[case.id] = draft
        results.append(Result(f"{case.id}: produced a valid scorecard", True))
        results.extend(check_structure(case, draft))

        for quality in check_quality(case, draft):
            if comparative:
                results.append(quality)
            else:
                results.append(
                    Result(
                        quality.name,
                        True,
                        "needs a real model -- a fixed response cannot demonstrate judgement",
                        skipped=True,
                    )
                )

    for comparison in COMPARISONS:
        name = f"{comparison.id}: {comparison.stronger} > {comparison.weaker} on {comparison.dimension}"
        if not comparative:
            results.append(Result(name, True, comparison.rationale, skipped=True))
            continue

        strong, weak = drafts.get(comparison.stronger), drafts.get(comparison.weaker)
        if strong is None or weak is None:
            results.append(Result(name, False, "a case failed to score"))
            continue

        s, w = dimension(strong, comparison.dimension), dimension(weak, comparison.dimension)
        results.append(Result(name, s > w, f"{s} vs {w}"))

    width = max(len(r.name) for r in results) + 2
    for r in results:
        mark = SKIP if r.skipped else (TICK if r.ok else CROSS)
        line = f"  {mark}  {r.name.ljust(width)}"
        if r.detail and (not r.ok or r.skipped):
            line += f"{DIM}{r.detail[:70]}{RESET}"
        print(line)

    failed = [r for r in results if not r.ok and not r.skipped]
    passed = [r for r in results if r.ok and not r.skipped]
    skipped = [r for r in results if r.skipped]

    print(
        f"\n  {len(passed)} passed"
        + (f", {RED}{len(failed)} failed{RESET}" if failed else "")
        + (f", {len(skipped)} skipped" if skipped else "")
    )

    if drafts:
        print(f"\n  {DIM}scores by case:{RESET}")
        for case_id, draft in drafts.items():
            bars = " ".join(f"{d.name.split()[0][:4]}:{d.score}" for d in draft.dimensions)
            print(f"    {case_id.ljust(20)} overall {str(draft.overall).rjust(3)}   {DIM}{bars}{RESET}")

    print()
    return 1 if failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the Cadence scorer.")
    parser.add_argument(
        "--structural-only",
        action="store_true",
        help="Skip comparative checks even when an API key is available.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(comparative=not args.structural_only)))


if __name__ == "__main__":
    main()
