"""Storing and searching a session's job-description passages in PostgreSQL.

Two functions, and the shape of each is the safety property:

* :func:`store_job_description` runs once, when the session is created, inside
  the request's transaction. The description is split, embedded and written as
  passages; the original text is not kept anywhere.
* :func:`make_search` returns a search function already bound to one session.
  The tool handler calls it with a query and a count and nothing else, so there
  is no argument through which a model could reach another session's rows.

Ranking is pgvector's cosine distance (``<=>``) with no index: a description is
a few dozen passages, and the ``WHERE session_id = ...`` filter leaves an exact
scan of those rows. An HNSW or IVFFlat index trades recall for speed across
thousands of vectors, which is not this problem.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import JobDescriptionChunk, Vector
from app.retrieval.chunking import chunk_job_description
from app.retrieval.embedding import Embedder, HashingEmbedder

# A pasted description is capped in the request schema; this caps what one can
# turn into, so a pathological input cannot become thousands of rows.
MAX_PASSAGES = 64

Search = Callable[[str, int], Awaitable[list[dict[str, Any]]]]

_default_embedder = HashingEmbedder()


def get_embedder() -> Embedder:
    """The embedder in use. One place to swap in a hosted model."""
    return _default_embedder


async def store_job_description(
    db: AsyncSession, session_id: uuid.UUID, description: str, embedder: Embedder | None = None
) -> int:
    """Split, embed and insert. Returns how many passages were stored."""
    embedder = embedder or get_embedder()
    passages = chunk_job_description(description)[:MAX_PASSAGES]
    vectors = embedder.embed([p.text for p in passages])
    stored = 0
    for passage, vector in zip(passages, vectors):
        # A passage with no usable words embeds to the zero vector, and cosine
        # distance to a zero vector is undefined. It could never be found, so
        # it is not stored.
        if not any(vector):
            continue
        db.add(
            JobDescriptionChunk(
                session_id=session_id, ordinal=passage.ordinal, content=passage.text, embedding=vector
            )
        )
        stored += 1
    if stored:
        await db.flush()
    return stored


async def has_job_description(db: AsyncSession, session_id: uuid.UUID) -> bool:
    found = await db.scalar(
        select(func.count())
        .select_from(JobDescriptionChunk)
        .where(JobDescriptionChunk.session_id == session_id)
    )
    return bool(found)


_SEARCH = text(
    """
    SELECT ordinal, content, 1 - (embedding <=> CAST(:query AS vector)) AS score
    FROM job_description_chunks
    WHERE session_id = :session_id
    ORDER BY embedding <=> CAST(:query AS vector), ordinal
    LIMIT :k
    """
)

_to_pgvector = Vector().bind_processor(None)


def make_search(
    factory: async_sessionmaker[AsyncSession],
    session_id: uuid.UUID,
    embedder: Embedder | None = None,
) -> Search:
    """A search function fixed to ``session_id``, opening its own connection."""
    embedder = embedder or get_embedder()

    async def search(query: str, k: int) -> list[dict[str, Any]]:
        (vector,) = embedder.embed([query])
        if not any(vector):
            return []
        async with factory() as db:
            rows = await db.execute(
                _SEARCH, {"query": _to_pgvector(vector), "session_id": session_id, "k": k}
            )
            return [
                {"ordinal": r.ordinal, "text": r.content, "score": round(float(r.score), 3)}
                for r in rows
            ]

    return search
