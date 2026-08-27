"""Cadence — the backend package.

The only thing here is a version guard, and it earns its place.

The README carries a "Python 3.12" badge and CI pins 3.12, but nothing was
*enforcing* it. On 3.10 the app does not refuse to start with a sentence about
Python versions — it dies five imports deep on

    ImportError: cannot import name 'UTC' from 'datetime'

which is `datetime.UTC`, added in 3.11 and used by app/security.py and
app/routers/sessions.py. Someone meeting that error has to read the traceback,
find the import, know when UTC landed, and only then work out that the badge in
the README was a requirement rather than a decoration.

A badge is a claim. This is the check behind it.
"""

from __future__ import annotations

import sys

MINIMA = (3, 11)

if sys.version_info < MINIMA:
    atual = ".".join(str(n) for n in sys.version_info[:3])
    pedida = ".".join(str(n) for n in MINIMA)
    raise RuntimeError(
        f"Cadence precisa de Python {pedida} ou superior; este é {atual}.\n"
        f"O motivo concreto é datetime.UTC, que só existe a partir do 3.11 e é "
        f"usado em app/security.py e app/routers/sessions.py.\n"
        f"O Dockerfile e o CI usam 3.12, que é a versão em que isto é testado."
    )
