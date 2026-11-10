"""Configuração 3: agente (a especialização comparada ao RAG simples).

O RAG simples faz uma busca com a pergunta do jeito que veio e responde. O agente
usa a mesma LLM local e a mesma base, mas decide o que fazer:

1. PLANEJAR  A LLM reescreve a pergunta em até 3 consultas (uma por assunto) e diz
             se a pergunta pede a situação atual de um bairro ou rua.
2. AGIR      Ferramentas: ``buscar_base`` (uma busca híbrida por consulta, mais a
             pergunta original) e ``consultar_mapa`` (dados do produto).
3. RESPONDER Geração fundamentada com os melhores trechos reunidos (até 5).
4. VERIFICAR Checagens determinísticas da resposta: todo número (valor, prazo,
             telefone) aparece nos trechos entregues? A evidência citada existe
             nos trechos, ou a resposta é feita das palavras deles?
5. REPETIR   Se os trechos não responderam, ou a verificação falhou, o agente tem
             mais uma rodada: replaneja com outras palavras ou regenera com a
             crítica. Se ainda assim não fechar, recusa (falha fechada).

Cada passo fica registrado em ``Resposta.passos``: é a explicação de como a
resposta foi construída.
"""
from __future__ import annotations

import re
import time

from . import config, lgpd, llm, mapa
from .busca import Indice
from .geracao import (
    PAPEL,
    RECUSA_PADRAO,
    SISTEMA_RAG,
    Resposta,
    apoio_lexical,
    esquema_rag,
    interpretar_saida_fundamentada,
    mensagem_usuario,
    montar_contexto,
    resumo_resultado,
)

MAX_TRECHOS = 5
MAX_RODADAS = 2
# fração mínima das palavras de conteúdo da resposta que precisa estar nos trechos citados
APOIO_MINIMO = 0.6

SISTEMA_PLANEJAR = (
    "Você prepara buscas para um assistente que orienta moradores de São Paulo sobre alagamentos. "
    "A base tem documentos oficiais: orientações da Defesa Civil e do Ministério da Saúde, leis e decretos "
    "(FGTS por desastre, isenção de IPTU por enchente), serviços da Prefeitura e alertas por celular.\n\n"
    "Dada a PERGUNTA do morador, devolva um JSON com:\n"
    "- \"consultas\": de 1 a 3 consultas de busca curtas e objetivas, com os termos que um documento oficial "
    "usaria. Uma consulta por assunto: se a pergunta tem dois pedidos, faça duas consultas. "
    "Não responda à pergunta.\n"
    "- \"local\": se a pergunta quer saber como estão AGORA os bueiros, o risco ou o alagamento de um bairro, "
    "distrito ou rua específica, escreva só o nome do lugar; caso contrário, deixe \"\".\n\n"
    "Exemplos:\n"
    "PERGUNTA: entrou agua em casa e estragou a geladeira, tenho algum desconto no imposto?\n"
    "{\"consultas\": [\"isenção de IPTU imóvel atingido por enchente\", \"danos em eletrodomésticos enchente benefício\"], \"local\": \"\"}\n"
    "PERGUNTA: tem bueiro entupido no Grajaú hoje?\n"
    "{\"consultas\": [\"boca de lobo entupida o que fazer\"], \"local\": \"Grajaú\"}\n"
    "PERGUNTA: posso beber a agua da torneira depois da enchente e como limpo a caixa dagua?\n"
    "{\"consultas\": [\"água para beber depois da enchente tratamento\", \"limpeza da caixa d'água após enchente\"], \"local\": \"\"}"
)

ESQUEMA_PLANEJAR = {
    "type": "object",
    "properties": {
        "consultas": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
        "local": {"type": "string"},
    },
    "required": ["consultas", "local"],
}

SISTEMA_AGENTE = SISTEMA_RAG + (
    "\n8. Trechos com id começando por \"mapa#\" são dados atuais do mapa do Boca de Lobo: use-os para "
    "perguntas sobre a situação de um bairro ou rua, sempre dizendo de quando são os dados."
)

RE_NUMERO = re.compile(r"\d+(?:[.,]\d+)*")


def _numeros(texto: str) -> set[str]:
    """Números do texto, sem separador de milhar/decimal e sem zeros decimais à direita."""
    achados = set()
    for n in RE_NUMERO.findall(texto):
        n = re.sub(r",0+$", "", n)  # 6.220,00 -> 6.220
        achados.add(n.replace(".", "").replace(",", ""))
    return achados


def numeros_sem_apoio(resposta: str, textos: list[str], pergunta: str) -> list[str]:
    """Números que aparecem na resposta mas não nos trechos citados nem na pergunta."""
    permitidos = _numeros(" ".join(textos)) | _numeros(pergunta)

    def apoiado(n: str) -> bool:
        # "20 mil" na resposta e "20.000" no trecho são o mesmo número
        return n in permitidos or any(p in (n + "000", n + "000000") for p in permitidos)

    return sorted(n for n in _numeros(resposta) if len(n) > 1 and not apoiado(n))


def planejar(pergunta: str, critica: str = "") -> tuple[list[str], str, llm.SaidaLLM]:
    usuario = f"PERGUNTA: {pergunta}"
    if critica:
        usuario += f"\n\nObservação: {critica}"
    saida = llm.conversar(
        [{"role": "system", "content": SISTEMA_PLANEJAR}, {"role": "user", "content": usuario}],
        esquema=ESQUEMA_PLANEJAR,
        max_tokens=160,
    )
    dados = saida.dados or {}
    consultas = [c.strip() for c in dados.get("consultas", []) if isinstance(c, str) and c.strip()][:3]
    local = dados.get("local", "") if isinstance(dados.get("local"), str) else ""
    return consultas, local.strip(), saida


def responder_agente(pergunta: str, indice: Indice, limiar: float | None = None) -> Resposta:
    inicio = time.perf_counter()
    limiar = config.limiar_recusa() if limiar is None else limiar
    pergunta_limpa, tipos = lgpd.mascarar(pergunta.strip())
    resp = Resposta("agente", pergunta_limpa, True, RECUSA_PADRAO, limiar=limiar)
    resp.dados_pessoais_mascarados = tipos

    reunidos: dict[str, tuple[float, object]] = {}  # id -> (melhor cosseno, trecho)
    trechos_mapa: dict[str, object] = {}
    consultas_feitas: list[str] = []
    confianca = 0.0
    critica = ""

    def buscar_base(consulta: str) -> None:
        nonlocal confianca
        vetor = indice.vetor_consulta(consulta)
        resultados = indice.buscar(consulta, k=config.TOP_K, vetor=vetor)
        conf = indice.confianca(consulta, vetor=vetor)
        confianca = max(confianca, conf)
        for r in resultados:
            anterior = reunidos.get(r.trecho.id, (0.0, None))[0]
            reunidos[r.trecho.id] = (max(anterior, r.cosseno), r.trecho)
        consultas_feitas.append(consulta)
        resp.passos.append(
            {
                "passo": "buscar_base",
                "consulta": consulta,
                "confianca": round(conf, 4),
                "trechos": [resumo_resultado(r)["id"] for r in resultados],
            }
        )
        resp.recuperados.extend(resumo_resultado(r) for r in resultados)

    buscar_base(pergunta_limpa)  # a pergunta original sempre é buscada

    for rodada in range(1, MAX_RODADAS + 1):
        # 1. planejar (na 2ª rodada, só se o problema foi falta de trecho) ------
        consultas, local = [], ""
        if not critica.startswith("VERIFICACAO"):
            consultas, local, saida = planejar(pergunta_limpa, critica)
            resp.chamadas_llm += 1
            resp.modelo = saida.modelo
            resp.passos.append({"passo": "planejar", "rodada": rodada, "consultas": consultas, "local": local})

        # 2. agir --------------------------------------------------------------
        for consulta in consultas:
            if consulta.lower() not in {c.lower() for c in consultas_feitas}:
                buscar_base(consulta)
        if local and not trechos_mapa:
            consulta_mapa = mapa.consultar(local)
            t = mapa.como_trecho(consulta_mapa)
            trechos_mapa[t.id] = t
            resp.passos.append(
                {
                    "passo": "consultar_mapa",
                    "local": local,
                    "pontos_encontrados": consulta_mapa.encontrados,
                    "origem": consulta_mapa.origem,
                    "dados_de": consulta_mapa.gerado_em,
                }
            )
        resp.confianca = round(confianca, 4)

        if confianca < limiar and not trechos_mapa:
            resp.motivo = "abaixo_do_limiar"
            resp.passos.append({"passo": "recusar", "motivo": resp.motivo, "confianca": resp.confianca})
            break

        # 3. responder ---------------------------------------------------------
        melhores = sorted(reunidos.values(), key=lambda x: -x[0])[: MAX_TRECHOS - len(trechos_mapa)]
        disponiveis = {**trechos_mapa, **{t.id: t for _, t in melhores}}
        mensagens = [
            {"role": "system", "content": SISTEMA_AGENTE},
            {"role": "user", "content": mensagem_usuario(pergunta_limpa, montar_contexto(list(disponiveis.values())))},
        ]
        if critica.startswith("VERIFICACAO"):
            mensagens.append({"role": "user", "content": critica})
        saida = llm.conversar(mensagens, esquema=esquema_rag(list(disponiveis)))
        resp.chamadas_llm += 1
        interpretar_saida_fundamentada(resp, saida, disponiveis)
        resp.passos.append(
            {
                "passo": "responder",
                "rodada": rodada,
                "trechos_no_contexto": list(disponiveis),
                "resultado": resp.motivo,
            }
        )

        # 4. verificar ---------------------------------------------------------
        if resp.recusa:
            if rodada < MAX_RODADAS and resp.motivo in {"modelo_recusou", "resposta_sem_fonte"}:
                critica = (
                    "As consultas anteriores ("
                    + "; ".join(consultas_feitas)
                    + ") não trouxeram a resposta. Proponha consultas diferentes, com sinônimos e termos oficiais."
                )
                continue
            break
        # números: valem os que estão em qualquer trecho entregue ao modelo (texto, título ou seção)
        contexto_completo = [f"{t.titulo} {t.secao} {t.texto}" for t in disponiveis.values()]
        sem_apoio = numeros_sem_apoio(resp.resposta, contexto_completo, pergunta_limpa)
        # texto: a evidência confere ou a própria resposta é feita das palavras dos trechos citados
        apoio = apoio_lexical(resp.resposta, [f"{f['titulo']} {f['secao']} {f['texto']}" for f in resp.fontes])
        aprovado = not sem_apoio and (bool(resp.evidencia_confere) or apoio >= APOIO_MINIMO)
        resp.passos.append(
            {
                "passo": "verificar",
                "rodada": rodada,
                "evidencia_confere": bool(resp.evidencia_confere),
                "apoio_lexical": round(apoio, 2),
                "numeros_sem_apoio": sem_apoio,
                "aprovado": aprovado,
            }
        )
        if aprovado:
            resp.motivo = "respondido_com_fonte_verificado"
            break
        if rodada < MAX_RODADAS:
            problemas = []
            if sem_apoio:
                problemas.append("os números " + ", ".join(sem_apoio) + " não aparecem nos trechos citados")
            if not problemas:
                problemas.append("a resposta usa informação que não está escrita nos trechos citados")
            critica = (
                "VERIFICACAO: a resposta anterior foi reprovada porque "
                + " e ".join(problemas)
                + ". Refaça usando só o que está escrito nos trechos; se eles não respondem, recuse."
            )
            continue
        # 5. não fechou em duas rodadas: falha fechada
        resp.recusa, resp.resposta, resp.fontes = True, RECUSA_PADRAO, []
        resp.motivo = "reprovado_na_verificacao"
        resp.passos.append({"passo": "recusar", "motivo": resp.motivo})

    resp.tempo_s = round(time.perf_counter() - inicio, 2)
    return resp


__all__ = ["responder_agente", "planejar", "numeros_sem_apoio", "PAPEL"]
