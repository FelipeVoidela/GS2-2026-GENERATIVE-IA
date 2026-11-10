"""API do assistente (FastAPI). É o contrato com o restante do produto.

    uvicorn boca_de_lobo_ia.api:app --app-dir src --port 8000

Rotas:
    GET  /            tela de conversa (web/index.html)
    GET  /saude       situação do serviço e dos modelos locais
    GET  /fontes      catálogo da base (fonte, licença, número de trechos)
    POST /perguntar   {"pergunta": "...", "modo": "agente" | "rag" | "sem_contexto"}

Segurança e LGPD (ver README):
    * a LLM roda localmente; a pergunta não vai para serviço externo;
    * identificadores pessoais digitados são mascarados antes da busca, da LLM e do log;
    * o log guarda só a pergunta mascarada e metadados, sem IP nem identificador do morador;
    * tamanho de entrada limitado, limite de requisições por minuto e chave opcional (X-API-Key);
    * a saída é sempre um JSON de formato fixo; resposta sem fonte não é entregue.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from collections import Counter, defaultdict, deque
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__, agente, config, geracao, llm
from .busca import Indice

AVISO = (
    "Assistente informativo: não substitui a Defesa Civil (199), os Bombeiros (193) nem um serviço de saúde. "
    "Não informe nome, CPF, telefone ou endereço completo."
)
_estado: dict = {"indice": None}
_trava_llm = threading.Lock()  # uma geração por vez: o modelo roda em CPU
_trava_log = threading.Lock()
_janelas: dict[str, deque] = defaultdict(deque)


def indice() -> Indice:
    if _estado["indice"] is None:
        _estado["indice"] = Indice()
    return _estado["indice"]


@asynccontextmanager
async def ciclo(_: FastAPI):
    try:
        indice()  # carrega trechos e embeddings na subida
    except Exception:  # noqa: BLE001 - a API sobe mesmo assim e o /saude mostra o problema
        _estado["indice"] = None
    yield


app = FastAPI(
    title="Boca de Lobo · assistente do morador",
    version=__version__,
    description="RAG e agente sobre fontes oficiais de proteção e defesa civil, com LLM local.",
    lifespan=ciclo,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.API_ORIGENS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key"],
)


class Pergunta(BaseModel):
    pergunta: str = Field(min_length=3, max_length=config.API_MAX_PERGUNTA, description="Pergunta do morador")
    modo: Literal["agente", "rag", "sem_contexto"] = "agente"


def autorizar(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    """Chave opcional e limite de requisições por minuto, por origem."""
    if config.API_CHAVE and x_api_key != config.API_CHAVE:
        raise HTTPException(status_code=401, detail="Chave de API ausente ou inválida.")
    origem = request.client.host if request.client else "?"
    agora = time.time()
    janela = _janelas[origem]
    while janela and agora - janela[0] > 60:
        janela.popleft()
    if len(janela) >= config.API_LIMITE_POR_MINUTO:
        raise HTTPException(status_code=429, detail="Muitas perguntas em pouco tempo. Tente de novo em um minuto.")
    janela.append(agora)


def registrar(resposta: geracao.Resposta) -> None:
    """Log mínimo para acompanhar a qualidade: nada que identifique o morador."""
    registro = {
        "quando": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "modo": resposta.configuracao,
        "pergunta": resposta.pergunta,  # já mascarada
        "recusa": resposta.recusa,
        "motivo": resposta.motivo,
        "fontes": [f.get("id") for f in resposta.fontes],
        "confianca": resposta.confianca,
        "tempo_s": resposta.tempo_s,
    }
    try:
        with _trava_log:
            config.ARQ_LOG_API.parent.mkdir(parents=True, exist_ok=True)
            with config.ARQ_LOG_API.open("a", encoding="utf-8") as f:
                f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    except OSError:
        pass  # falha de log não derruba a resposta


@app.exception_handler(llm.ErroLLM)
async def llm_fora(_: Request, erro: llm.ErroLLM) -> JSONResponse:
    # degradação com transparência: o morador recebe o caminho alternativo
    return JSONResponse(
        status_code=503,
        content={
            "detail": "O modelo local está indisponível no momento.",
            "orientacao": "Em emergência, ligue 199 (Defesa Civil) ou 193 (Bombeiros).",
            "erro": str(erro)[:200],
        },
    )


@app.get("/", include_in_schema=False)
def pagina() -> FileResponse:
    return FileResponse(config.RAIZ / "web" / "index.html")


@app.get("/saude")
def saude() -> dict:
    modelos = llm.situacao()
    pronto = bool(
        _estado["indice"] is not None
        and modelos.get("ollama")
        and modelos.get("modelo_llm_disponivel")
        and modelos.get("modelo_embedding_disponivel")
    )
    return {
        "pronto": pronto,
        "versao": __version__,
        "trechos": len(_estado["indice"].trechos) if _estado["indice"] else 0,
        "limiar_recusa": config.limiar_recusa(),
        **modelos,
    }


@app.get("/fontes")
def fontes() -> list[dict]:
    contagem = Counter(t.fonte_id for t in indice().trechos)
    catalogo = json.loads(config.ARQ_FONTES.read_text(encoding="utf-8"))
    return [
        {
            "id": f["id"],
            "titulo": f["titulo"],
            "orgao": f["orgao"],
            "url": f["url"],
            "formato": f["formato"],
            "licenca": f["licenca"],
            "tipo_texto": f["tipo_texto"],
            "trechos": contagem.get(f["id"], 0),
        }
        for f in catalogo
    ]


@app.post("/perguntar", dependencies=[Depends(autorizar)])
def perguntar(entrada: Pergunta) -> dict:
    with _trava_llm:
        if entrada.modo == "agente":
            resposta = agente.responder_agente(entrada.pergunta, indice())
        elif entrada.modo == "rag":
            resposta = geracao.responder_rag(entrada.pergunta, indice())
        else:
            resposta = geracao.responder_sem_contexto(entrada.pergunta)
    registrar(resposta)
    return {**resposta.para_dict(), "aviso": AVISO}
