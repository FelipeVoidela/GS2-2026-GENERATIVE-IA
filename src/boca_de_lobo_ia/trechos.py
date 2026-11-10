"""Divide os documentos de base/documentos em trechos com fonte (base/trechos.jsonl).

Regras, pensadas para o tamanho do contexto de uma LLM pequena rodando em CPU:

* cada seção (``## título``) vira um ou mais trechos; um trecho nunca mistura seções;
* tamanho alvo de 110 palavras e máximo de 170, cortando só em fim de parágrafo ou
  de item de lista (nunca no meio de uma frase);
* todo trecho carrega título do documento, seção, órgão, URL e licença, que é o
  que permite citar a fonte na resposta.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import config

ALVO_PALAVRAS = 110
MAX_PALAVRAS = 170
MIN_PALAVRAS = 12


@dataclass
class Trecho:
    id: str
    fonte_id: str
    titulo: str
    secao: str
    texto: str
    orgao: str
    url: str
    licenca: str
    tipo_texto: str
    palavras: int

    def para_busca(self) -> str:
        """Texto indexado: o título e a seção entram junto, porque dão o assunto."""
        return f"{self.titulo}. {self.secao}. {self.texto}" if self.secao else f"{self.titulo}. {self.texto}"


def ler_documento(caminho: Path) -> tuple[dict, list[tuple[str, list[str]]]]:
    """Devolve (metadados, [(seção, [parágrafos ou itens])])."""
    bruto = caminho.read_text(encoding="utf-8")
    _, cab, corpo = bruto.split("---", 2)
    meta = {}
    for linha in cab.strip().splitlines():
        chave, _, valor = linha.partition(":")
        meta[chave.strip()] = valor.strip()
    secoes: list[tuple[str, list[str]]] = [("", [])]
    for linha in corpo.splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("# ") or linha.startswith(">"):
            continue
        if linha.startswith("## "):
            secoes.append((linha[3:].strip(), []))
        else:
            secoes[-1][1].append(linha)
    return meta, [(s, p) for s, p in secoes if p]


def _n(texto: str) -> int:
    return len(texto.split())


def _fatiar_longo(paragrafo: str) -> list[str]:
    """Parágrafo acima do máximo: corta em fim de frase."""
    if _n(paragrafo) <= MAX_PALAVRAS:
        return [paragrafo]
    frases = re.split(r"(?<=[.;:!?])\s+", paragrafo)
    partes, atual = [], ""
    for f in frases:
        if atual and _n(atual) + _n(f) > MAX_PALAVRAS:
            partes.append(atual)
            atual = f
        else:
            atual = f"{atual} {f}".strip()
    if atual:
        partes.append(atual)
    return partes


def agrupar(paragrafos: list[str]) -> list[str]:
    """Junta parágrafos vizinhos até o tamanho alvo, sem passar do máximo."""
    unidades = [p for par in paragrafos for p in _fatiar_longo(par)]
    grupos: list[list[str]] = []
    for u in unidades:
        if grupos and _n(" ".join(grupos[-1])) < ALVO_PALAVRAS and _n(" ".join(grupos[-1])) + _n(u) <= MAX_PALAVRAS:
            grupos[-1].append(u)
        else:
            grupos.append([u])
    # sobra muito curta no fim da seção: cola no grupo anterior se couber
    if len(grupos) > 1 and _n(" ".join(grupos[-1])) < MIN_PALAVRAS * 2:
        if _n(" ".join(grupos[-2])) + _n(" ".join(grupos[-1])) <= MAX_PALAVRAS + 30:
            grupos[-2].extend(grupos.pop())
    return ["\n".join(g) for g in grupos]


def gerar_trechos() -> list[Trecho]:
    trechos: list[Trecho] = []
    for caminho in sorted(config.DIR_DOCUMENTOS.glob("*.md")):
        meta, secoes = ler_documento(caminho)
        n = 0
        for secao, paragrafos in secoes:
            for texto in agrupar(paragrafos):
                if _n(texto) < MIN_PALAVRAS and not re.search(r"\d", texto):
                    continue  # resto de menu ou título solto
                n += 1
                trechos.append(
                    Trecho(
                        id=f"{meta['id']}#{n:02d}",
                        fonte_id=meta["id"],
                        titulo=meta["titulo"],
                        secao=secao,
                        texto=texto,
                        orgao=meta["orgao"],
                        url=meta["url"],
                        licenca=meta["licenca"],
                        tipo_texto=meta["tipo_texto"],
                        palavras=_n(texto),
                    )
                )
    return trechos


def gravar(trechos: list[Trecho]) -> None:
    with config.ARQ_TRECHOS.open("w", encoding="utf-8", newline="\n") as f:
        for t in trechos:
            f.write(json.dumps(asdict(t), ensure_ascii=False) + "\n")


def carregar() -> list[Trecho]:
    with config.ARQ_TRECHOS.open(encoding="utf-8") as f:
        return [Trecho(**json.loads(linha)) for linha in f if linha.strip()]
