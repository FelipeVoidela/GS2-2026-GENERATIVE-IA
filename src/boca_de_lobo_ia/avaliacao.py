"""Avaliação: casos de teste com gabarito, recall da busca, limiar e placar.

O julgamento é automático e determinístico (mesma entrada, mesmo veredito), para que
qualquer pessoa consiga reproduzir o placar:

* a resposta é comparada com os ``pontos_chave`` do gabarito (expressões regulares
  sobre o texto sem acento). Cada ponto é uma lista de alternativas;
* nos casos "não consta", a resposta certa é a recusa;
* a citação é correta quando algum trecho citado está no gabarito do caso.

Limite conhecido: pontos-chave não medem redação nem pegam todo erro factual. Por
isso o relatório traz também a leitura manual de 5 erros.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from pathlib import Path

from . import config
from .busca import Indice, normalizar

VEREDITOS = ["correta", "parcial", "incorreta", "recusa_correta", "recusa_indevida", "alucinacao"]
CONFIGURACOES = ["sem_contexto", "rag", "agente"]
ROTULOS = {"sem_contexto": "1. LLM sem contexto", "rag": "2. RAG simples", "agente": "3. Agente (especialização)"}


# --------------------------------------------------------------------------- #
# casos
# --------------------------------------------------------------------------- #
def carregar_casos(particao: str | None = None) -> list[dict]:
    casos = []
    with config.ARQ_CASOS.open(encoding="utf-8") as f:
        for linha in f:
            if linha.strip():
                caso = json.loads(linha)
                if particao is None or caso["particao"] == particao:
                    casos.append(caso)
    return casos


def conferir_gabarito(casos: list[dict], indice: Indice) -> list[str]:
    """Problemas de consistência do gabarito (ids de trecho que não existem etc.)."""
    problemas = []
    vistos = set()
    for c in casos:
        if c["id"] in vistos:
            problemas.append(f"{c['id']}: id repetido")
        vistos.add(c["id"])
        for t in c.get("trechos_gabarito", []):
            if not t.startswith("mapa#") and t not in indice.por_id:
                problemas.append(f"{c['id']}: trecho do gabarito não existe na base: {t}")
        if c["consta"] and not c.get("pontos_chave"):
            problemas.append(f"{c['id']}: caso respondível sem pontos-chave")
        for grupo in c.get("pontos_chave", []):
            for alternativa in grupo:
                re.compile(alternativa)
    return problemas


# --------------------------------------------------------------------------- #
# julgamento de uma resposta
# --------------------------------------------------------------------------- #
def _casa(padrao: str, texto: str) -> bool:
    return re.search(padrao, texto) is not None


def julgar(caso: dict, resposta: dict) -> dict:
    """Compara a resposta (dict de ``Resposta.para_dict()``) com o gabarito do caso."""
    texto = normalizar(resposta.get("resposta", ""))
    recusou = bool(resposta.get("recusa"))
    grupos = caso.get("pontos_chave", [])
    atingidos = sum(1 for g in grupos if any(_casa(a, texto) for a in g))
    proibido = (not recusou) and any(_casa(p, texto) for p in caso.get("proibidos", []))
    ids_citados = [f.get("id") for f in resposta.get("fontes", []) if f.get("id")]
    gabarito = set(caso.get("trechos_gabarito", []))

    if not caso["consta"]:
        veredito = "recusa_correta" if recusou else "alucinacao"
    elif recusou:
        veredito = "recusa_correta" if caso.get("recusa_aceita") else "recusa_indevida"
    elif proibido:
        veredito = "incorreta"
    elif grupos and atingidos == len(grupos):
        veredito = "correta"
    elif atingidos > 0:
        veredito = "parcial"
    else:
        veredito = "incorreta"

    respondeu_caso_respondivel = caso["consta"] and not recusou
    return {
        "veredito": veredito,
        "acerto": veredito in {"correta", "recusa_correta"},
        # alucinou = entregou ao morador uma resposta errada ou que não podia existir
        "alucinou": veredito in {"alucinacao", "incorreta"},
        "pontos_atingidos": atingidos,
        "pontos_total": len(grupos),
        "citou": bool(ids_citados),
        "citacao_correta": (bool(gabarito & set(ids_citados)) if respondeu_caso_respondivel else None),
        "evidencia_confere": resposta.get("evidencia_confere") if respondeu_caso_respondivel else None,
    }


# --------------------------------------------------------------------------- #
# execução das configurações (com retomada)
# --------------------------------------------------------------------------- #
def arquivo_resultados(configuracao: str) -> Path:
    return config.DIR_RESULTADOS / f"{configuracao}.jsonl"


def carregar_resultados(configuracao: str) -> dict[str, dict]:
    caminho = arquivo_resultados(configuracao)
    if not caminho.exists():
        return {}
    saida = {}
    with caminho.open(encoding="utf-8") as f:
        for linha in f:
            if linha.strip():
                r = json.loads(linha)
                saida[r["id"]] = r
    return saida


def executar(configuracao: str, casos: list[dict], indice: Indice, limiar: float, ao_concluir=None) -> dict[str, dict]:
    """Roda uma configuração sobre os casos, gravando cada resposta assim que sai.

    Casos já gravados em avaliacao/resultados/<configuracao>.jsonl não são refeitos:
    em CPU a rodada completa leva mais de uma hora e pode ser retomada.
    """
    from . import agente, geracao

    funcoes = {
        "sem_contexto": lambda p: geracao.responder_sem_contexto(p),
        "rag": lambda p: geracao.responder_rag(p, indice, limiar=limiar),
        "agente": lambda p: agente.responder_agente(p, indice, limiar=limiar),
    }
    config.DIR_RESULTADOS.mkdir(parents=True, exist_ok=True)
    feitos = carregar_resultados(configuracao)
    with arquivo_resultados(configuracao).open("a", encoding="utf-8", newline="\n") as f:
        for caso in casos:
            if caso["id"] in feitos:
                continue
            resposta = funcoes[configuracao](caso["pergunta"]).para_dict()
            registro = {
                "id": caso["id"],
                "particao": caso["particao"],
                "categoria": caso["categoria"],
                "consta": caso["consta"],
                **resposta,
                "julgamento": julgar(caso, resposta),
            }
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
            f.flush()
            feitos[caso["id"]] = registro
            if ao_concluir:
                ao_concluir(configuracao, registro)
    return feitos


def rejulgar(configuracao: str, casos: list[dict]) -> dict[str, dict]:
    """Reaplica o julgamento às respostas já gravadas (se o gabarito mudou) e regrava."""
    por_id = {c["id"]: c for c in casos}
    resultados = carregar_resultados(configuracao)
    for rid, r in resultados.items():
        if rid in por_id:
            r["julgamento"] = julgar(por_id[rid], r)
    with arquivo_resultados(configuracao).open("w", encoding="utf-8", newline="\n") as f:
        for r in resultados.values():
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return resultados


# --------------------------------------------------------------------------- #
# métricas
# --------------------------------------------------------------------------- #
def _pct(parte: int, total: int) -> float | None:
    return round(100 * parte / total, 1) if total else None


def metricas(resultados: list[dict]) -> dict:
    n = len(resultados)
    j = [r["julgamento"] for r in resultados]
    respondiveis = [r for r in resultados if r["consta"]]
    nao_consta = [r for r in resultados if not r["consta"]]
    respondidas_ok = [x for x in j if x["citacao_correta"] is not None]
    tempos = [r["tempo_s"] for r in resultados]
    return {
        "casos": n,
        "acerto_pct": _pct(sum(x["acerto"] for x in j), n),
        "alucinacao_pct": _pct(sum(x["alucinou"] for x in j), n),
        "corretas_respondiveis_pct": _pct(
            sum(r["julgamento"]["veredito"] in {"correta", "recusa_correta"} for r in respondiveis), len(respondiveis)
        ),
        "parciais_pct": _pct(sum(x["veredito"] == "parcial" for x in j), n),
        "recusa_indevida_pct": _pct(
            sum(r["julgamento"]["veredito"] == "recusa_indevida" for r in respondiveis), len(respondiveis)
        ),
        "recusa_no_nao_consta_pct": _pct(
            sum(r["julgamento"]["veredito"] == "recusa_correta" for r in nao_consta), len(nao_consta)
        ),
        "json_valido_pct": _pct(sum(bool(r.get("json_valido")) for r in resultados), n),
        "citacao_correta_pct": _pct(sum(bool(x["citacao_correta"]) for x in respondidas_ok), len(respondidas_ok)),
        "evidencia_confere_pct": _pct(sum(bool(x["evidencia_confere"]) for x in respondidas_ok), len(respondidas_ok)),
        "tempo_medio_s": round(statistics.mean(tempos), 1) if tempos else None,
        "tempo_mediano_s": round(statistics.median(tempos), 1) if tempos else None,
        "chamadas_llm_media": round(statistics.mean(r.get("chamadas_llm", 0) for r in resultados), 2) if n else None,
    }


def placar(particao: str = "teste", excluir_categorias: tuple[str, ...] = ()) -> list[dict]:
    """Uma linha por configuração, sobre a partição pedida."""
    linhas = []
    for nome in CONFIGURACOES:
        rs = [
            r
            for r in carregar_resultados(nome).values()
            if r["particao"] == particao and r["categoria"] not in excluir_categorias
        ]
        if rs:
            linhas.append({"configuracao": ROTULOS[nome], **metricas(rs)})
    return linhas


def placar_por_categoria(particao: str = "teste") -> list[dict]:
    linhas = []
    for nome in CONFIGURACOES:
        grupos = defaultdict(list)
        for r in carregar_resultados(nome).values():
            if r["particao"] == particao:
                grupos[r["categoria"]].append(r)
        for categoria, rs in sorted(grupos.items()):
            m = metricas(rs)
            linhas.append(
                {
                    "configuracao": ROTULOS[nome],
                    "categoria": categoria,
                    "casos": m["casos"],
                    "acerto_pct": m["acerto_pct"],
                    "alucinacao_pct": m["alucinacao_pct"],
                }
            )
    return linhas


# --------------------------------------------------------------------------- #
# busca: recall@k e limiar
# --------------------------------------------------------------------------- #
def casos_de_busca(casos: list[dict]) -> list[dict]:
    """Casos em que a resposta está em algum trecho da base (exclui 'não consta' e mapa)."""
    return [c for c in casos if c["consta"] and not c.get("ferramenta")]


def recall(casos: list[dict], indice: Indice, modo: str, ks=(1, 3, 5), peso_vetorial: float | None = None) -> dict:
    """recall@k: fração dos casos com pelo menos um trecho do gabarito entre os k primeiros.

    Também devolve o MRR e, para as perguntas com dois assuntos, a cobertura completa
    (todos os assuntos com pelo menos um trecho recuperado).
    """
    alvo = casos_de_busca(casos)
    acertos = {k: 0 for k in ks}
    completos = {k: 0 for k in ks}
    multi = 0
    rr = 0.0
    detalhes = []
    for c in alvo:
        resultados = indice.buscar(c["pergunta"], k=max(ks), modo=modo, peso_vetorial=peso_vetorial)
        ids = [r.trecho.id for r in resultados]
        gabarito = set(c["trechos_gabarito"])
        posicao = next((i + 1 for i, t in enumerate(ids) if t in gabarito), None)
        rr += 1 / posicao if posicao else 0
        for k in ks:
            acertos[k] += bool(posicao and posicao <= k)
        if c.get("grupos_gabarito"):
            multi += 1
            for k in ks:
                completos[k] += all(set(g) & set(ids[:k]) for g in c["grupos_gabarito"])
        detalhes.append({"id": c["id"], "posicao": posicao, "recuperados": ids})
    n = len(alvo)
    saida = {"modo": modo, "casos": n, "mrr": round(rr / n, 3) if n else None}
    for k in ks:
        saida[f"recall@{k}"] = round(acertos[k] / n, 3) if n else None
    for k in ks:
        saida[f"cobertura_completa_multi@{k}"] = round(completos[k] / multi, 3) if multi else None
    saida["detalhes"] = detalhes
    return saida


def confiancas(casos: list[dict], indice: Indice) -> list[dict]:
    """Maior cosseno pergunta x base de cada caso (a variável do limiar)."""
    saida = []
    for c in casos:
        if c.get("ferramenta"):
            continue  # perguntas respondidas pelo mapa não dependem da base
        saida.append(
            {
                "id": c["id"],
                "particao": c["particao"],
                "consta": c["consta"],
                "fora_do_dominio": "Fora do domínio" in c.get("observacao", ""),
                "confianca": round(indice.confianca(c["pergunta"]), 4),
            }
        )
    return saida


def curva_limiar(pontos: list[dict], limiares: list[float]) -> list[dict]:
    """Para cada limiar: quanto de respondível é barrado e quanto de 'não consta' é barrado."""
    pos = [p["confianca"] for p in pontos if p["consta"]]
    neg = [p["confianca"] for p in pontos if not p["consta"]]
    linhas = []
    for t in limiares:
        linhas.append(
            {
                "limiar": round(t, 2),
                "respondiveis_barradas_pct": _pct(sum(x < t for x in pos), len(pos)),
                "nao_consta_barradas_pct": _pct(sum(x < t for x in neg), len(neg)),
            }
        )
    return linhas


def escolher_limiar(pontos_dev: list[dict], margem: float = 0.03) -> dict:
    """Limiar = menor confiança de uma pergunta respondível do conjunto de calibração, menos uma margem.

    Critério: o limiar é um filtro barato (sem LLM) para o que está claramente fora da
    base. Barrar uma pergunta respondível deixa o morador sem orientação, o que é pior
    do que deixar passar uma pergunta fora da base, porque essa ainda encontra a segunda
    barreira (a regra de recusa na geração). Por isso o limiar fica abaixo de todas as
    respondíveis da calibração.
    """
    pos = [p["confianca"] for p in pontos_dev if p["consta"]]
    neg = [p["confianca"] for p in pontos_dev if not p["consta"]]
    limiar = int((min(pos) - margem) * 100) / 100
    return {
        "limiar": limiar,
        "menor_confianca_respondivel": min(pos),
        "margem": margem,
        "nao_consta_barradas_na_calibracao": sum(x < limiar for x in neg),
        "nao_consta_na_calibracao": len(neg),
        "respondiveis_na_calibracao": len(pos),
    }
