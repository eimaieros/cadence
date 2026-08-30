#!/usr/bin/env python3
"""Hold the docs to the number of tests that actually ran.

WHY THIS EXISTS.

This repository states a test count in three places and they mean three
different things:

    README.md      badge                  backend + frontend
    README.md      "the SSE parser has N" frontend only
    CONTRIBUTING   next to `pytest -q`    backend only

Nothing was re-measuring any of them, and they drifted apart exactly as you
would expect. On one commit the badge said 85 while CONTRIBUTING said 44 -- the
second was the backend count from before the observability tests existed, and
had been wrong for a week.

A number typed into prose is a measurement with no instrument behind it. This is
the instrument, and it is scope-aware on purpose: asserting that every number in
the README equals one grand total would be simpler and would be wrong here,
because two of the three are deliberately partial.

The counts come in as arguments rather than being measured here. The backend
suite needs a live PostgreSQL and the frontend suite needs node_modules; they
run in separate CI jobs, and the honest number is the one those runs printed,
not one this script could produce by re-collecting under different conditions.

    python3 tools/contagem.py --backend 81 --frontend 20
    python3 tools/contagem.py --backend 81 --frontend 20 --fix
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backend", type=int, required=True, help="testes que o pytest correu")
    p.add_argument("--frontend", type=int, required=True, help="testes que o vitest correu")
    p.add_argument("--fix", action="store_true", help="reescrever os documentos")
    a = p.parse_args()

    if a.backend <= 0 or a.frontend <= 0:
        # Um zero aqui quase de certeza significa que a extraccao do numero
        # falhou em CI, e nao que a suite desapareceu. Passar em silencio seria
        # a verificacao a desligar-se sozinha.
        print(f"contagem: contagens implausiveis (backend={a.backend}, frontend={a.frontend}).")
        print("        Provavelmente o passo que as extrai deixou de encontrar o numero.")
        return 1

    total = a.backend + a.frontend

    # (ficheiro, padrao com um grupo, valor esperado, o que a frase quer dizer)
    alegacoes = [
        ("README.md", re.compile(r"badge/tests-(\d+)-"), total, "cracha: total"),
        ("README.md", re.compile(r"!\[(\d+) tests\]"), total, "texto do cracha: total"),
        ("README.md", re.compile(r"SSE parser has (\d+) tests"), a.frontend, "so o frontend"),
        ("CONTRIBUTING.md", re.compile(r"(\d+) tests, needs a real PostgreSQL"), a.backend, "so o backend"),
    ]

    errado = 0
    for nome, padrao, esperado, significado in alegacoes:
        caminho = RAIZ / nome
        if not caminho.exists():
            continue
        texto = caminho.read_text(encoding="utf-8")
        m = padrao.search(texto)
        if not m:
            print(f"contagem: {nome} deixou de dizer o numero ({padrao.pattern}).")
            errado += 1
            continue
        if int(m.group(1)) == esperado:
            continue
        if a.fix:
            novo = texto[: m.start(1)] + str(esperado) + texto[m.end(1) :]
            caminho.write_text(novo, encoding="utf-8")
            print(f"contagem: {nome} [{significado}] {m.group(1)} -> {esperado}")
        else:
            print(f"contagem: {nome} [{significado}] diz {m.group(1)}; sao {esperado}.")
            errado += 1

    if errado:
        print(f"        Corrige com:  python3 tools/contagem.py --backend {a.backend} "
              f"--frontend {a.frontend} --fix")
        return 1
    if not a.fix:
        print(f"contagem: {a.backend} backend + {a.frontend} frontend = {total} — bate certo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
