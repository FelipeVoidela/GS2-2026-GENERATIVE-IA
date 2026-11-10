"""Configuração central. Tudo que muda entre ambientes vem de variável de ambiente
(ver .env.example); nada de chave ou endereço fixo espalhado pelo código."""
from __future__ import annotations

import os
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]

# --- pastas -----------------------------------------------------------------
DIR_BASE = RAIZ / "base"
DIR_DOCUMENTOS = DIR_BASE / "documentos"
DIR_BRUTO = DIR_BASE / "bruto"  # cópias baixadas das fontes (fora do Git)
DIR_INDICE = DIR_BASE / "indice"
ARQ_FONTES = DIR_BASE / "fontes.json"
ARQ_TRECHOS = DIR_BASE / "trechos.jsonl"
ARQ_EMBEDDINGS = DIR_INDICE / "embeddings.npy"
ARQ_EMBEDDINGS_META = DIR_INDICE / "embeddings_meta.json"

DIR_AVALIACAO = RAIZ / "avaliacao"
ARQ_CASOS = DIR_AVALIACAO / "casos_teste.jsonl"
DIR_RESULTADOS = DIR_AVALIACAO / "resultados"
ARQ_LIMIAR = DIR_AVALIACAO / "limiar.json"

DIR_DADOS = RAIZ / "dados"
ARQ_MAPA_LOCAL = DIR_DADOS / "mapa_cidadao.json"

# --- LLM local (Ollama) -----------------------------------------------------
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
MODELO_LLM = os.getenv("MODELO_LLM", "gemma3:4b")
MODELO_EMBEDDING = os.getenv("MODELO_EMBEDDING", "bge-m3")
TEMPERATURA = float(os.getenv("TEMPERATURA", "0"))
SEMENTE = int(os.getenv("SEMENTE", "42"))
NUM_CTX = int(os.getenv("NUM_CTX", "4096"))
MAX_TOKENS_RESPOSTA = int(os.getenv("MAX_TOKENS_RESPOSTA", "400"))
TIMEOUT_LLM_S = float(os.getenv("TIMEOUT_LLM_S", "600"))

# --- busca ------------------------------------------------------------------
TOP_K = int(os.getenv("TOP_K", "3"))
# Peso da busca vetorial na fusão híbrida (1 - PESO_VETORIAL vai para o BM25).
PESO_VETORIAL = float(os.getenv("PESO_VETORIAL", "0.6"))
# Limiar de recusa: similaridade de cosseno do melhor trecho. O valor padrão é o
# calibrado no notebook 02 (gravado em avaliacao/limiar.json); a variável de
# ambiente só existe para experimentos.
LIMIAR_PADRAO = float(os.getenv("LIMIAR_RECUSA", "0.50"))

# --- integração com o produto (componente Dados) ----------------------------
# Export público da camada Ouro (ouro_vw_mapa_cidadao), sem dado pessoal.
MAPA_URL = os.getenv(
    "MAPA_URL",
    "https://felipevoidela.github.io/GS2-2026-Big-Data-Architecture/dados/mapa_cidadao.json",
)

# --- API --------------------------------------------------------------------
API_CHAVE = os.getenv("API_CHAVE", "")  # vazio = sem autenticação (uso local)
API_ORIGENS = [o.strip() for o in os.getenv("API_ORIGENS", "*").split(",") if o.strip()]
API_MAX_PERGUNTA = int(os.getenv("API_MAX_PERGUNTA", "500"))
API_LIMITE_POR_MINUTO = int(os.getenv("API_LIMITE_POR_MINUTO", "20"))
ARQ_LOG_API = Path(os.getenv("ARQ_LOG_API", str(RAIZ / "logs" / "perguntas.jsonl")))


def limiar_recusa() -> float:
    """Limiar em uso: o calibrado (avaliacao/limiar.json) ou o padrão."""
    import json

    if os.getenv("LIMIAR_RECUSA"):
        return LIMIAR_PADRAO
    if ARQ_LIMIAR.exists():
        try:
            return float(json.loads(ARQ_LIMIAR.read_text(encoding="utf-8"))["limiar"])
        except (ValueError, KeyError):
            pass
    return LIMIAR_PADRAO
