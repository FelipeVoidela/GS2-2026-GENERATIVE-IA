"""Minimização de dados pessoais (LGPD, art. 6º, III: necessidade).

O assistente não precisa saber quem é o morador para responder. Antes de a pergunta
ser usada (busca, LLM) e antes de qualquer registro em log, os identificadores
diretos que o morador possa ter digitado são trocados por marcadores.

O que NÃO é feito, e por quê: nomes próprios não são detectados (exigiria um modelo
de NER e erraria em nomes de rua, que a pergunta precisa manter). Por isso a tela
avisa o morador para não informar dados pessoais.
"""
from __future__ import annotations

import re

PADROES = [
    ("[EMAIL]", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("[CPF]", re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")),
    ("[CNPJ]", re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")),
    # telefone com DDD, com ou sem +55, com ou sem o 9 (não casa 193, 199, 156, 40199)
    ("[TELEFONE]", re.compile(r"(?<!\d)(?:\+?55\s?)?\(?\d{2}\)?\s?9?\d{4}[-\s]?\d{4}(?!\d)")),
    ("[CEP]", re.compile(r"\b\d{5}-\d{3}\b")),
    ("[RG]", re.compile(r"\b\d{1,2}\.\d{3}\.\d{3}-[\dxX]\b")),
]


def mascarar(texto: str) -> tuple[str, list[str]]:
    """Devolve o texto com os identificadores trocados e a lista de tipos encontrados."""
    tipos: list[str] = []
    for marcador, padrao in PADROES:
        texto, n = padrao.subn(marcador, texto)
        if n:
            tipos.append(marcador.strip("[]"))
    return texto, tipos
