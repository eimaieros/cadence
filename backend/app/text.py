"""Text the database will accept.

PostgreSQL cannot store U+0000 in ``TEXT`` or ``JSONB``. Left alone, one NUL in
a request body travels all the way to the INSERT, the driver raises, and the
caller gets a 500 while the logs get a stack trace -- a malformed input
reported as our failure, and one anybody can trigger at will.

Every string that is stored goes through :data:`StoredText` instead, so a NUL
is refused at the boundary as a 422, like any other malformed input.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator


def reject_nul(value: str) -> str:
    if "\x00" in value:
        raise ValueError("text cannot contain NUL characters")
    return value


StoredText = Annotated[str, AfterValidator(reject_nul)]
