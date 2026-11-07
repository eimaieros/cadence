"""Splitting a job description into passages that can be searched.

Job descriptions are mostly lists: "Requirements", then six bullets; "Nice to
have", then four more. That shape decides the splitting:

* **A bullet is never cut in half.** "3+ years with PostgreSQL" split after
  "3+ years with" retrieves nothing useful from either side. Paragraphs and list
  items are the units, and units are packed whole.
* **A heading travels with what follows it.** "Nice to have" is what makes the
  bullet under it mean something different from the same bullet under
  "Requirements", so a heading opens the passage its items go into.
* **Passages overlap by one item.** The last unit of a passage starts the next
  one, when it is short, so an item that sits on a boundary is still found next
  to its neighbour.
* **Only text that is too long on its own gets cut**, at a sentence boundary
  if there is one, at a space if there is not.

Character counts rather than tokens: the target only needs to be roughly right,
and counting characters needs no tokenizer and gives the same answer everywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Around a short paragraph or four bullets. Small enough that a passage is about
# one thing; big enough that it still carries its heading and some context.
TARGET_CHARS = 500
# A single unit longer than this is split before packing.
MAX_UNIT_CHARS = 800
# The unit carried over into the next passage, only when it is this short.
# Overlap that repeats a whole paragraph costs more than it finds.
MAX_OVERLAP_CHARS = 200

_BULLET = re.compile(r"^\s*(?:[-*•▪◦·–]|\d{1,2}[.)])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Passage:
    """One searchable piece of a job description, in document order."""

    ordinal: int
    text: str


def _is_heading(line: str) -> bool:
    """Short and without a full stop: "Requirements", "What you'll do:"."""
    stripped = line.strip()
    return 0 < len(stripped) <= 60 and not stripped.endswith(".") and not _BULLET.match(line)


def _units(text: str) -> list[tuple[str, bool]]:
    """Paragraphs, list items and headings, each kept whole.

    Returns ``(text, is_heading)`` pairs. Consecutive plain lines are one
    paragraph; every bullet is its own unit; a blank line ends a paragraph.
    """
    units: list[tuple[str, bool]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            units.append((" ".join(paragraph), False))
            paragraph.clear()

    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            flush()
            continue
        if _BULLET.match(raw):
            flush()
            units.append((_BULLET.sub("", raw).strip(), False))
        elif _is_heading(raw) and not paragraph:
            units.append((line.rstrip(":").strip(), True))
        else:
            paragraph.append(line)
    flush()
    return units


def _split_long(unit: str) -> list[str]:
    """Cut a unit that is too long on its own, preferring sentence boundaries."""
    if len(unit) <= MAX_UNIT_CHARS:
        return [unit]
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(unit):
        while len(sentence) > MAX_UNIT_CHARS:
            cut = sentence.rfind(" ", 0, MAX_UNIT_CHARS)
            cut = cut if cut > 0 else MAX_UNIT_CHARS
            if current:
                pieces.append(current)
                current = ""
            pieces.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > MAX_UNIT_CHARS:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return [p for p in pieces if p]


def chunk_job_description(text: str) -> list[Passage]:
    """Split ``text`` into passages of roughly :data:`TARGET_CHARS`.

    Empty or whitespace-only input gives no passages rather than one empty one,
    so a session without a description simply has nothing to search.
    """
    units: list[tuple[str, bool]] = []
    for unit, is_heading in _units(text):
        units.extend((piece, is_heading) for piece in _split_long(unit))

    passages: list[str] = []
    current: list[str] = []
    length = 0

    def close() -> None:
        if current:
            passages.append("\n".join(current))

    for unit, is_heading in units:
        too_long = length and length + 1 + len(unit) > TARGET_CHARS
        if is_heading or too_long:
            # Headings open a passage. Carrying the last item into the next
            # passage only makes sense inside a list, not across a heading.
            carry = (
                current[-1]
                if too_long and not is_heading and current and len(current[-1]) <= MAX_OVERLAP_CHARS
                else None
            )
            close()
            current = [carry] if carry and len(current) > 1 else []
            length = sum(len(u) + 1 for u in current)
        current.append(unit)
        length += len(unit) + 1
    close()

    return [Passage(ordinal=i, text=p) for i, p in enumerate(passages)]
