"""Turning passages and queries into vectors.

Two embedders behind one interface, for the same reason there are two
interviewers: the app has to run, and be tested, with no API key.

:class:`HashingEmbedder` needs nothing. It hashes words, word pairs and short
character runs into a fixed number of buckets -- the "hashing trick" -- and
normalises the result, so cosine similarity becomes a measure of shared
vocabulary. That is honest about what it is: it finds "PostgreSQL" when the
query says "Postgres", because they share character runs, but it does not know
that "relational database" and "PostgreSQL" mean nearly the same thing. A
learned embedding model does, which is why one is used when a key is present.

Two properties matter more here than quality, and both are tested:

* **Deterministic across processes.** Python's ``hash()`` is salted per process,
  so it would give different vectors on every restart and silently break every
  stored embedding. BLAKE2 is not salted.
* **Normalised.** Every vector has length 1, so the cosine distance pgvector
  computes is a fair comparison between passages of different lengths.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Sequence
from typing import Protocol

# At 384 buckets, collisions between unrelated words in a short query were as
# large as the real overlap ("PostgreSQL experience" sat closer to "A yearly
# team trip" than to a passage about the Postgres schema). 1024 separates them;
# test_related_texts_are_closer_than_unrelated_ones holds the line.
DIMENSIONS = 1024

# Keeps "c++", "c#", "node.js" and "next.js" as one token each.
_TOKEN = re.compile(r"[a-z0-9]+(?:[.+#][a-z0-9+#]*)*")

# Words that appear in almost every job ad and so tell passages apart least.
_STOPWORDS = frozenset(
    """
    a about all an and are as at be by can for from have in is it its of on or our
    so that the their this to we what will with you your who
    s t d ll re ve
    """.split()
)

# Weights per feature kind. Whole words carry the meaning; pairs keep a little
# word order ("not required"); character runs bridge spelling variants.
_WORD_WEIGHT = 1.0
_PAIR_WEIGHT = 0.5
_CHARS_WEIGHT = 0.3
_CHAR_RUN = 4


class Embedder(Protocol):
    """Anything that turns texts into vectors of a fixed length."""

    dimensions: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return [t.strip(".") for t in _TOKEN.findall(folded) if t.strip(".") not in _STOPWORDS]


def _bucket(feature: str, dimensions: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    # The lowest bit picks the sign, so two features that collide in a bucket
    # cancel out on average instead of always adding up.
    return (value >> 1) % dimensions, (1.0 if value & 1 else -1.0)


class HashingEmbedder:
    """A dependency-free embedder for offline runs and tests."""

    def __init__(self, dimensions: int = DIMENSIONS) -> None:
        self.dimensions = dimensions

    def _features(self, text: str) -> list[tuple[str, float]]:
        tokens = _tokens(text)
        features = [(f"w:{t}", _WORD_WEIGHT) for t in tokens]
        features += [(f"p:{a} {b}", _PAIR_WEIGHT) for a, b in zip(tokens, tokens[1:])]
        for t in tokens:
            padded = f"<{t}>"
            features += [
                (f"c:{padded[i : i + _CHAR_RUN]}", _CHARS_WEIGHT)
                for i in range(max(1, len(padded) - _CHAR_RUN + 1))
            ]
        return features

    def embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for feature, weight in self._features(text):
            index, sign = _bucket(feature, self.dimensions)
            vector[index] += sign * weight
        norm = math.sqrt(sum(v * v for v in vector))
        # A text with no usable words embeds to the zero vector. It matches
        # nothing, which is the right answer, and never divides by zero.
        return [v / norm for v in vector] if norm else vector

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; 0.0 when either vector is all zeros."""
    # strict: two vectors of different lengths are a bug upstream, and a dot
    # product over the shorter prefix would hide it behind a plausible score.
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def rank(query: Sequence[float], passages: Sequence[Sequence[float]], k: int) -> list[tuple[int, float]]:
    """Indices of the ``k`` passages closest to ``query``, best first.

    The in-memory version of what pgvector's ``<=>`` operator does in SQL. The
    retrieval evaluation uses it, so the numbers it reports do not depend on a
    database being up.
    """
    scored = [(i, cosine_similarity(query, p)) for i, p in enumerate(passages)]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[:k]
