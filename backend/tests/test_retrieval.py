"""Job-description retrieval: chunking, the offline embedder and ranking.

None of this touches the database. The parts tested here decide *what* is
stored and *how* it is compared; storing it is tested with the rest of the
schema, against real PostgreSQL.
"""

from __future__ import annotations

import math
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.retrieval.chunking import (
    MAX_UNIT_CHARS,
    TARGET_CHARS,
    chunk_job_description,
)
from app.retrieval.embedding import DIMENSIONS, HashingEmbedder, cosine_similarity, rank
from app.retrieval.store import store_job_description
from evals.retrieval import evaluate, mean_reciprocal_rank, recall_at
from evals.retrieval_cases import CASES

LISTING = """\
Requirements
- 3+ years building backend services in Python
- Strong SQL and PostgreSQL
- Experience with message queues

Nice to have
- Some Go
"""


# --- chunking ---------------------------------------------------------------


def test_empty_description_gives_no_passages():
    assert chunk_job_description("") == []
    assert chunk_job_description("   \n\n \t") == []


def test_bullets_are_never_cut_in_half():
    passages = chunk_job_description(LISTING)
    text = "\n".join(p.text for p in passages)
    for bullet in (
        "3+ years building backend services in Python",
        "Strong SQL and PostgreSQL",
        "Experience with message queues",
        "Some Go",
    ):
        assert any(bullet in p.text for p in passages), bullet
    assert "- " not in text  # bullet markers are dropped, the item is kept


def test_a_heading_opens_the_passage_its_items_go_into():
    passages = chunk_job_description(LISTING)
    nice = next(p for p in passages if "Some Go" in p.text)
    assert nice.text.startswith("Nice to have")
    assert "Strong SQL" not in nice.text


def test_passages_stay_near_the_target_and_keep_document_order():
    items = "\n".join(f"- Requirement number {n} about something specific" for n in range(40))
    passages = chunk_job_description("Requirements\n" + items)
    assert len(passages) > 1
    assert [p.ordinal for p in passages] == list(range(len(passages)))
    for p in passages:
        assert len(p.text) <= TARGET_CHARS + MAX_UNIT_CHARS


def test_consecutive_passages_overlap_by_one_short_item():
    items = "\n".join(f"- Requirement number {n} about something specific" for n in range(40))
    passages = chunk_job_description("Requirements\n" + items)
    first, second = passages[0].text.split("\n"), passages[1].text.split("\n")
    assert first[-1] == second[0]


def test_an_overlong_unit_is_split_at_sentence_boundaries():
    sentence = "This sentence is part of a very long paragraph about the team. "
    paragraph = sentence * 40  # well over MAX_UNIT_CHARS
    passages = chunk_job_description(paragraph)
    assert len(passages) > 1
    for p in passages:
        assert len(p.text) <= MAX_UNIT_CHARS
        assert p.text.endswith(".")


def test_windows_line_endings_are_handled():
    assert chunk_job_description(LISTING.replace("\n", "\r\n")) == chunk_job_description(LISTING)


# --- embedding --------------------------------------------------------------


def test_vectors_have_the_configured_length_and_unit_norm():
    [vector] = HashingEmbedder().embed(["Python services on FastAPI"])
    assert len(vector) == DIMENSIONS
    assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


def test_text_without_words_embeds_to_zero_and_matches_nothing():
    [empty] = HashingEmbedder().embed(["--- !!! ---"])
    assert not any(empty)
    assert cosine_similarity(empty, empty) == 0.0


def test_embedding_is_the_same_in_a_fresh_process():
    """Salted ``hash()`` would pass an in-process check and break every stored vector."""
    here = HashingEmbedder().embed_one("PostgreSQL query tuning")[:8]
    code = (
        "from app.retrieval.embedding import HashingEmbedder;"
        "print(HashingEmbedder().embed_one('PostgreSQL query tuning')[:8])"
    )
    backend = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=backend, capture_output=True, text=True, check=True
    ).stdout
    assert out.strip() == str(here)


def test_related_texts_are_closer_than_unrelated_ones():
    embedder = HashingEmbedder()
    query, related, unrelated = embedder.embed(
        ["PostgreSQL experience", "Own the Postgres schema and query tuning", "A yearly team trip"]
    )
    assert cosine_similarity(query, related) > cosine_similarity(query, unrelated)


def test_spelling_variants_meet_through_character_runs():
    embedder = HashingEmbedder()
    query, variant, other = embedder.embed(["Postgres", "PostgreSQL", "Kubernetes"])
    assert cosine_similarity(query, variant) > cosine_similarity(query, other)


def test_rank_returns_best_first_and_breaks_ties_by_order():
    query = [1.0, 0.0]
    passages = [[0.0, 1.0], [1.0, 0.0], [1.0, 0.0]]
    assert [i for i, _ in rank(query, passages, k=3)] == [1, 2, 0]
    assert len(rank(query, passages, k=1)) == 1


# --- evaluation -------------------------------------------------------------


def test_every_expected_phrase_is_in_exactly_one_passage():
    """A phrase in two passages would let a wrong answer count as right."""
    for case in CASES:
        passages = chunk_job_description(case.description)
        for query in case.queries:
            hits = [p for p in passages if query.expected in p.text]
            assert len(hits) == 1, (case.name, query.expected, len(hits))


def test_offline_retrieval_meets_its_floor_on_standard_queries():
    from evals.retrieval import MRR_FLOOR, RECALL_AT_3_FLOOR

    standard = [r for r in evaluate(CASES, HashingEmbedder()) if not r.hard]
    assert recall_at(standard, 3) >= RECALL_AT_3_FLOOR
    assert mean_reciprocal_rank(standard) >= MRR_FLOOR


def test_vectors_of_different_lengths_are_an_error_not_a_prefix():
    with pytest.raises(ValueError):
        cosine_similarity([1.0, 0.0, 0.0], [1.0, 0.0])


async def test_an_embedder_that_returns_too_few_vectors_fails_loudly():
    """A hosted embedder that drops a vector must not shorten the description quietly."""

    class ShortEmbedder:
        def embed(self, texts):
            return HashingEmbedder().embed(texts)[:-1]

    class Db:
        def add(self, row):
            pass

        async def flush(self):
            pass

    with pytest.raises(ValueError):
        await store_job_description(Db(), uuid.uuid4(), LISTING, embedder=ShortEmbedder())
