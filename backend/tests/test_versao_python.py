"""The Python version is declared in four places. Keep them from disagreeing.

WHY THIS FILE EXISTS.

The version this project needs is written down in the README badge, in the
Dockerfile, in the CI workflow, and now in app/__init__.py. Four copies of one
fact, none of which had anything checking it, is the exact shape of every bug
this repository and its siblings turned up in August: a number that was true
when it was typed and started decaying immediately.

It already half-happened. The badge said 3.12 and CI said 3.12 and neither was
enforced anywhere, so the app ran happily on 3.10 right up until it hit
`datetime.UTC` five imports deep and produced an ImportError that said nothing
about Python versions.

These tests need no database and no network, so they run everywhere the suite
runs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]
BACKEND = RAIZ / "backend"
GUARDA = BACKEND / "app" / "__init__.py"


def _versao(texto: str, padrao: str) -> tuple[int, ...]:
    m = re.search(padrao, texto)
    assert m, f"nao encontrei {padrao!r}"
    return tuple(int(n) for n in m.group(1).split("."))


def _minima() -> tuple[int, ...]:
    """MINIMA lido do ficheiro, não importado dele.

    `from app import MINIMA` parece o caminho óbvio e é uma armadilha: o guarda
    levanta no import, por isso num Python demasiado antigo — precisamente o
    caso que estes testes descrevem — o ficheiro de teste rebentaria a recolher
    em vez de explicar. Ler o texto deixa isto correr em qualquer versão.
    """
    m = re.search(r"^MINIMA\s*=\s*\((\d+),\s*(\d+)\)", GUARDA.read_text(), re.M)
    assert m, "app/__init__.py deixou de declarar MINIMA como tuplo literal"
    return (int(m.group(1)), int(m.group(2)))


MINIMA = _minima()


def test_o_dockerfile_nao_corre_abaixo_do_minimo():
    docker = _versao((BACKEND / "Dockerfile").read_text(), r"FROM python:(\d+\.\d+)")
    assert docker >= MINIMA, (
        f"o Dockerfile usa {docker} e o guarda em app/__init__.py exige {MINIMA}: "
        "a imagem recusaria arrancar"
    )


def test_o_ci_nao_corre_abaixo_do_minimo():
    fluxos = list((RAIZ / ".github" / "workflows").glob("*.yml"))
    assert fluxos, "sem workflows para verificar"
    encontrou = False
    for f in fluxos:
        for bruto in re.findall(r"python-version:\s*['\"]?(\d+\.\d+)", f.read_text()):
            encontrou = True
            versao = tuple(int(n) for n in bruto.split("."))
            assert versao >= MINIMA, f"{f.name} testa em {versao}, abaixo de {MINIMA}"
    assert encontrou, "nenhum workflow diz em que Python corre"


def test_o_cracha_do_readme_diz_a_versao_em_que_isto_e_testado():
    """O crachá não é o mínimo: é a versão em que o projeto é de facto testado.

    Por isso compara-se com o CI e com o Dockerfile, e não com MINIMA. Um crachá
    que anuncia uma versão que nada testa é publicidade.
    """
    cracha = _versao((RAIZ / "README.md").read_text(), r"python-(\d+\.\d+)-blue")
    docker = _versao((BACKEND / "Dockerfile").read_text(), r"FROM python:(\d+\.\d+)")
    ci = _versao(
        (RAIZ / ".github" / "workflows" / "ci.yml").read_text(),
        r"python-version:\s*['\"]?(\d+\.\d+)",
    )
    assert cracha == docker == ci, (
        f"o README anuncia {cracha}, o Dockerfile constroi em {docker} e o CI "
        f"testa em {ci} — pelo menos um deles esta a mentir"
    )


def test_o_guarda_dispara_abaixo_do_minimo(monkeypatch):
    """Reexecuta app/__init__.py com uma versão fingida, para ver o guarda a agir.

    Sem isto o guarda é código que ninguém correu: o CI corre sempre numa versão
    onde ele nunca dispara.
    """
    fonte = (BACKEND / "app" / "__init__.py").read_text()
    baixo = (MINIMA[0], MINIMA[1] - 1, 0)

    import types

    espaco: dict = {"sys": types.SimpleNamespace(version_info=baixo)}
    with pytest.raises(RuntimeError, match=r"Python \d+\.\d+ ou superior"):
        exec(compile(fonte.replace("import sys", ""), "app/__init__.py", "exec"), espaco)
