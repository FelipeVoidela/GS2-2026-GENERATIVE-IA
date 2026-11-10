"""Busca nos trechos da base: BM25 (lexical), vetorial (embeddings) e híbrida.

* BM25: implementação própria, sem dependência externa, sobre tokens sem acento e
  sem palavras vazias. Acerta quando a pergunta usa o termo exato ("FGTS", "IPTU").
* Vetorial: embeddings multilíngues (bge-m3, local, via Ollama) e similaridade de
  cosseno. Acerta quando o morador usa outras palavras ("minha rua virou um rio").
* Híbrida: fusão dos dois rankings por Reciprocal Rank Fusion (RRF) ponderada.

A confiança usada no limiar de recusa é a similaridade de cosseno do melhor trecho
(não a nota da fusão), porque o cosseno tem escala fixa e comparável entre perguntas.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

import numpy as np

from . import config, llm
from .trechos import Trecho, carregar

PALAVRAS_VAZIAS = set(
    """a o as os um uma uns umas de do da dos das em no na nos nas por para pra pro com sem sob sobre entre
    e ou mas que se como quando onde qual quais quem cujo porque pois ja nao sim mais menos muito muita pouco
    eu tu ele ela nos eles elas voce voces me te lhe meu minha meus minhas seu sua seus suas esse essa isso este
    esta isto aquele aquela aquilo ao aos à às pelo pela pelos pelas num numa dum duma ser estar ter haver foi
    sao era tem tinha esta estao fica ficar vai vou pode posso podem deve devo devem fazer faco faz feito
    tambem so ate apos antes depois durante la ai aqui ali entao caso tipo cada todo toda todos todas
    algum alguma alguns algumas nenhum nenhuma outro outra outros outras mesmo mesma""".split()
)
K_RRF = 60


def normalizar(texto: str) -> str:
    """Minúsculas e sem acento (para comparar texto do morador com texto oficial)."""
    sem = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in sem if unicodedata.category(c) != "Mn")


def _radical(token: str) -> str:
    """Redução bem leve de plural, só para casar 'bueiros' com 'bueiro'."""
    if len(token) > 4 and token.endswith("oes"):
        return token[:-3] + "ao"
    if len(token) > 4 and token.endswith("es") and token[-3] in "rzl":
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenizar(texto: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", normalizar(texto))
    return [_radical(t) for t in tokens if t not in PALAVRAS_VAZIAS and (len(t) > 1 or t.isdigit())]


class BM25:
    """Okapi BM25 clássico."""

    def __init__(self, documentos: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(documentos)
        self.tamanhos = [len(d) for d in documentos]
        self.media = sum(self.tamanhos) / max(self.n, 1)
        self.frequencias = [Counter(d) for d in documentos]
        df: Counter = Counter()
        for d in documentos:
            df.update(set(d))
        self.idf = {t: math.log(1 + (self.n - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def pontuar(self, consulta: list[str]) -> np.ndarray:
        notas = np.zeros(self.n)
        for i, freq in enumerate(self.frequencias):
            norma = self.k1 * (1 - self.b + self.b * self.tamanhos[i] / self.media)
            total = 0.0
            for t in consulta:
                f = freq.get(t, 0)
                if f:
                    total += self.idf.get(t, 0.0) * f * (self.k1 + 1) / (f + norma)
            notas[i] = total
        return notas


@dataclass
class Resultado:
    trecho: Trecho
    posicao: int
    nota: float  # nota do modo de busca usado (RRF, BM25 ou cosseno)
    cosseno: float
    bm25: float


def _assinatura(trechos: list[Trecho], modelo: str) -> str:
    h = hashlib.sha256(modelo.encode())
    for t in trechos:
        h.update(t.id.encode())
        h.update(t.para_busca().encode())
    return h.hexdigest()[:20]


class Indice:
    """Índice em memória dos trechos (BM25 + matriz de embeddings normalizada)."""

    def __init__(self, trechos: list[Trecho] | None = None, usar_vetores: bool = True):
        self.trechos = trechos if trechos is not None else carregar()
        self.por_id = {t.id: t for t in self.trechos}
        self.bm25 = BM25([tokenizar(t.para_busca()) for t in self.trechos])
        self.vetores: np.ndarray | None = None
        if usar_vetores:
            self.vetores = self._carregar_ou_gerar_vetores()

    # -- embeddings ---------------------------------------------------------
    def _carregar_ou_gerar_vetores(self) -> np.ndarray:
        assinatura = _assinatura(self.trechos, config.MODELO_EMBEDDING)
        if config.ARQ_EMBEDDINGS.exists() and config.ARQ_EMBEDDINGS_META.exists():
            meta = json.loads(config.ARQ_EMBEDDINGS_META.read_text(encoding="utf-8"))
            if meta.get("assinatura") == assinatura:
                return np.load(config.ARQ_EMBEDDINGS)
        vetores = np.array(llm.embeddings([t.para_busca() for t in self.trechos]), dtype=np.float32)
        vetores /= np.linalg.norm(vetores, axis=1, keepdims=True)
        config.DIR_INDICE.mkdir(parents=True, exist_ok=True)
        np.save(config.ARQ_EMBEDDINGS, vetores)
        config.ARQ_EMBEDDINGS_META.write_text(
            json.dumps(
                {
                    "assinatura": assinatura,
                    "modelo": config.MODELO_EMBEDDING,
                    "trechos": len(self.trechos),
                    "dimensao": int(vetores.shape[1]),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return vetores

    def vetor_consulta(self, consulta: str) -> np.ndarray:
        v = np.array(llm.embeddings([consulta])[0], dtype=np.float32)
        return v / np.linalg.norm(v)

    # -- busca --------------------------------------------------------------
    def notas(self, consulta: str, vetor: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
        """(notas BM25, cossenos) de todos os trechos para a consulta."""
        bm = self.bm25.pontuar(tokenizar(consulta))
        if self.vetores is None:
            return bm, np.zeros(len(self.trechos))
        v = vetor if vetor is not None else self.vetor_consulta(consulta)
        return bm, self.vetores @ v

    def buscar(
        self,
        consulta: str,
        k: int | None = None,
        modo: str = "hibrida",
        peso_vetorial: float | None = None,
        vetor: np.ndarray | None = None,
    ) -> list[Resultado]:
        k = k or config.TOP_K
        w = config.PESO_VETORIAL if peso_vetorial is None else peso_vetorial
        bm, cos = self.notas(consulta, vetor) if modo != "bm25" else (
            self.bm25.pontuar(tokenizar(consulta)),
            np.zeros(len(self.trechos)),
        )
        if modo == "bm25":
            final = bm
        elif modo == "vetorial":
            final = cos
        elif modo == "hibrida":
            pos_bm = np.empty(len(bm), dtype=int)
            pos_bm[np.argsort(-bm, kind="stable")] = np.arange(len(bm))
            pos_cos = np.empty(len(cos), dtype=int)
            pos_cos[np.argsort(-cos, kind="stable")] = np.arange(len(cos))
            final = w / (K_RRF + 1 + pos_cos) + (1 - w) / (K_RRF + 1 + pos_bm)
            final = np.where(bm > 0, final, w / (K_RRF + 1 + pos_cos))  # sem termo em comum: só o vetorial conta
        else:
            raise ValueError(f"modo de busca desconhecido: {modo}")
        ordem = np.argsort(-final, kind="stable")[:k]
        return [
            Resultado(self.trechos[i], n + 1, float(final[i]), float(cos[i]), float(bm[i]))
            for n, i in enumerate(ordem)
        ]

    def confianca(self, consulta: str, vetor: np.ndarray | None = None) -> float:
        """Maior cosseno entre a consulta e qualquer trecho da base."""
        if self.vetores is None:
            return 0.0
        v = vetor if vetor is not None else self.vetor_consulta(consulta)
        return float((self.vetores @ v).max())
