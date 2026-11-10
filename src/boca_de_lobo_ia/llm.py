"""Cliente mínimo do Ollama (LLM e embeddings rodando localmente).

Nenhum dado sai da máquina: o modelo roda no container/serviço local apontado por
OLLAMA_URL. A saída estruturada usa o parâmetro ``format`` do Ollama com um JSON
Schema, que restringe a decodificação ao formato pedido.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import requests

from . import config


class ErroLLM(RuntimeError):
    """Falha de comunicação com o Ollama (serviço fora do ar, modelo ausente...)."""


@dataclass
class SaidaLLM:
    texto: str
    dados: dict | None  # JSON já interpretado, ou None se o texto não for JSON válido
    tempo_s: float
    tokens_entrada: int = 0
    tokens_saida: int = 0
    modelo: str = ""
    bruto: dict = field(default_factory=dict, repr=False)


def _post(rota: str, corpo: dict, timeout: float) -> dict:
    try:
        r = requests.post(f"{config.OLLAMA_URL}{rota}", json=corpo, timeout=timeout)
    except requests.RequestException as erro:
        raise ErroLLM(f"Ollama inacessível em {config.OLLAMA_URL}: {erro}") from erro
    if r.status_code != 200:
        raise ErroLLM(f"Ollama respondeu {r.status_code}: {r.text[:300]}")
    return r.json()


def conversar(
    mensagens: list[dict],
    esquema: dict | None = None,
    modelo: str | None = None,
    max_tokens: int | None = None,
    temperatura: float | None = None,
) -> SaidaLLM:
    """Uma chamada de chat. Com ``esquema``, a resposta é um JSON que o segue."""
    corpo = {
        "model": modelo or config.MODELO_LLM,
        "messages": mensagens,
        "stream": False,
        "options": {
            "temperature": config.TEMPERATURA if temperatura is None else temperatura,
            "seed": config.SEMENTE,
            "num_ctx": config.NUM_CTX,
            "num_predict": max_tokens or config.MAX_TOKENS_RESPOSTA,
        },
    }
    if esquema is not None:
        corpo["format"] = esquema
    inicio = time.perf_counter()
    resp = _post("/api/chat", corpo, config.TIMEOUT_LLM_S)
    tempo = time.perf_counter() - inicio
    texto = (resp.get("message") or {}).get("content", "")
    dados = None
    if esquema is not None:
        try:
            dados = json.loads(texto)
            if not isinstance(dados, dict):
                dados = None
        except json.JSONDecodeError:
            dados = None
    return SaidaLLM(
        texto=texto,
        dados=dados,
        tempo_s=round(tempo, 2),
        tokens_entrada=resp.get("prompt_eval_count", 0),
        tokens_saida=resp.get("eval_count", 0),
        modelo=corpo["model"],
        bruto=resp,
    )


def embeddings(textos: list[str], modelo: str | None = None, lote: int = 16) -> list[list[float]]:
    """Vetores dos textos, na mesma ordem."""
    saida: list[list[float]] = []
    for i in range(0, len(textos), lote):
        resp = _post(
            "/api/embed",
            {"model": modelo or config.MODELO_EMBEDDING, "input": textos[i : i + lote]},
            config.TIMEOUT_LLM_S,
        )
        saida.extend(resp["embeddings"])
    return saida


def situacao() -> dict:
    """Usado pelo /saude da API: o Ollama responde e os modelos estão baixados?"""
    try:
        r = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        r.raise_for_status()
        nomes = {m["name"] for m in r.json().get("models", [])}
    except requests.RequestException as erro:
        return {"ollama": False, "erro": str(erro), "modelos": []}

    def tem(nome: str) -> bool:
        return nome in nomes or f"{nome}:latest" in nomes

    return {
        "ollama": True,
        "modelo_llm": config.MODELO_LLM,
        "modelo_llm_disponivel": tem(config.MODELO_LLM),
        "modelo_embedding": config.MODELO_EMBEDDING,
        "modelo_embedding_disponivel": tem(config.MODELO_EMBEDDING),
    }
