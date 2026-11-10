"""Configuração 3: agente (a especialização comparada ao RAG simples).

O RAG simples faz uma busca com a pergunta do jeito que veio e responde. O agente
usa a mesma LLM local e a mesma base, mas decide o que fazer:

1. PLANEJAR  A LLM reescreve a pergunta em até 3 consultas (uma por assunto) e diz
             se a pergunta pede a situação atual de um bairro ou rua.
2. AGIR      Ferramentas: ``consultar_mapa`` (dados do produto, quando há um
             lugar na pergunta) e ``buscar_base`` (uma busca híbrida por consulta
             planejada, além da busca com a pergunta original).
3. RESPONDER Geração fundamentada. O contexto parte do que o RAG simples usaria (os
             3 trechos da pergunta original) e acrescenta o primeiro trecho de cada
             consulta planejada que ainda não esteja nele (até 4), mais o dado do
             mapa quando houver.
4. VERIFICAR Checagens determinísticas: todo número da resposta (valor, prazo,
             telefone) aparece nos trechos entregues? A evidência citada existe
             neles, ou ao menos parte da resposta vem deles?
5. REPETIR   Se a verificação reprova, ou a saída vem inválida ou sem fonte, o
             agente gera de novo, com a crítica. Se não fechar na segunda
             tentativa, recusa (falha fechada).

Cada passo fica registrado em ``Resposta.passos``: é a explicação de como a
resposta foi construída.

Três decisões deste desenho vieram dos erros medidos na partição dev (notebook 04):

* contexto enxuto: com 5 trechos parecidos, o modelo de 4 bilhões de parâmetros
  errava perguntas que o RAG simples, com 3, acertava;
* o limiar de recusa olha a pergunta original, não as consultas reescritas:
  reescrever com "termos oficiais" aproxima da base até a pergunta que não consta;
* recusa do modelo é respeitada: insistir com outras consultas depois de o modelo
  dizer que os trechos não respondem não recuperou nenhuma resposta e produziu uma
  alucinação.
"""
from __future__ import annotations

import re
import time

from . import config, lgpd, llm, mapa
from .busca import Indice, normalizar
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

MAX_TRECHOS = 5  # com o dado do mapa
MAX_TRECHOS_BASE = 4  # 3 da pergunta original + 1 de consulta planejada
MAX_TENTATIVAS = 2
# fração mínima das palavras de conteúdo da resposta que precisa estar nos trechos entregues
# (só é exigida quando a evidência apontada pelo modelo não confere)
APOIO_MINIMO = 0.35
# a saída do agente pode ser maior que a do RAG simples (mais trechos e o texto do mapa)
MAX_TOKENS_AGENTE = 600

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
    "\n9. Trechos com id começando por \"mapa#\" são dados atuais do mapa do Boca de Lobo: use-os para "
    "perguntas sobre a situação de um bairro ou rua, sempre dizendo de quando são os dados."
)

# --------------------------------------------------------------------------- #
# verificação de números
# --------------------------------------------------------------------------- #
RE_NUMERO = re.compile(r"\d+(?:[.,]\d+)*")
POR_EXTENSO = {
    "dez": 10, "onze": 11, "doze": 12, "treze": 13, "quatorze": 14, "catorze": 14, "quinze": 15,
    "dezesseis": 16, "dezessete": 17, "dezoito": 18, "dezenove": 19, "vinte": 20, "trinta": 30,
    "quarenta": 40, "cinquenta": 50, "sessenta": 60, "setenta": 70, "oitenta": 80, "noventa": 90,
    "cem": 100, "duzentos": 200, "quinhentos": 500, "mil": 1000,
}
RE_EXTENSO = re.compile(r"\b(" + "|".join(POR_EXTENSO) + r")\b(\s+mil\b)?")


def _numeros(texto: str) -> set[str]:
    """Números do texto em dígitos, sem separador e sem zeros decimais (6.220,00 vira 6220)."""
    achados = set()
    for n in RE_NUMERO.findall(texto):
        n = re.sub(r",0+$", "", n)
        achados.add(n.replace(".", "").replace(",", ""))
    return achados


def _numeros_das_fontes(texto: str) -> set[str]:
    """Números que as fontes afirmam, em dígitos ou por extenso ("doze meses", "cinquenta mil")."""
    achados = _numeros(texto)
    plano = normalizar(texto)
    for palavra, mil in RE_EXTENSO.findall(plano):
        valor = POR_EXTENSO[palavra] * (1000 if mil else 1)
        achados.add(str(valor))
    for n in re.findall(r"(\d+)\s+mil\b", plano):  # "R$ 20 mil"
        achados.add(str(int(n) * 1000))
    if "meia hora" in plano:
        achados.add("30")
    return achados


def numeros_sem_apoio(resposta: str, textos: list[str], pergunta: str) -> list[str]:
    """Números de dois ou mais dígitos que estão na resposta mas não nos trechos nem na pergunta."""
    permitidos = _numeros_das_fontes(" ".join(textos)) | _numeros_das_fontes(pergunta)

    def apoiado(n: str) -> bool:
        # "20 mil" na resposta e "20.000" no trecho são o mesmo número
        return n in permitidos or any(p in (n + "000", n + "000000") for p in permitidos)

    return sorted(n for n in _numeros(resposta) if len(n) > 1 and not apoiado(n))


# --------------------------------------------------------------------------- #
# passos do agente
# --------------------------------------------------------------------------- #
def planejar(pergunta: str) -> tuple[list[str], str, llm.SaidaLLM]:
    saida = llm.conversar(
        [{"role": "system", "content": SISTEMA_PLANEJAR}, {"role": "user", "content": f"PERGUNTA: {pergunta}"}],
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

    def encerrar() -> Resposta:
        resp.tempo_s = round(time.perf_counter() - inicio, 2)
        return resp

    def buscar_base(consulta: str) -> list:
        vetor = indice.vetor_consulta(consulta)
        resultados = indice.buscar(consulta, k=config.TOP_K, vetor=vetor)
        conf = indice.confianca(consulta, vetor=vetor)
        resp.passos.append(
            {
                "passo": "buscar_base",
                "consulta": consulta,
                "confianca": round(conf, 4),
                "trechos": [r.trecho.id for r in resultados],
            }
        )
        resp.recuperados.extend(resumo_resultado(r) for r in resultados)
        if consulta == pergunta_limpa:
            resp.confianca = round(conf, 4)
        return [r.trecho for r in resultados]

    # A pergunta original é sempre buscada; é a confiança dela que o limiar olha.
    base = buscar_base(pergunta_limpa)

    # 1. planejar ------------------------------------------------------------
    consultas, local, saida = planejar(pergunta_limpa)
    resp.chamadas_llm += 1
    resp.modelo = saida.modelo
    resp.passos.append({"passo": "planejar", "consultas": consultas, "local": local})

    # 2. agir: mapa -----------------------------------------------------------
    trechos_mapa: dict[str, object] = {}
    if local:
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

    if (resp.confianca or 0.0) < limiar and not trechos_mapa:
        resp.motivo = "abaixo_do_limiar"
        resp.passos.append({"passo": "recusar", "motivo": resp.motivo, "confianca": resp.confianca})
        return encerrar()

    # 2. agir: buscas planejadas ----------------------------------------------
    for consulta in consultas:
        if consulta.lower() == pergunta_limpa.lower():
            continue
        achados = buscar_base(consulta)
        if achados and achados[0].id not in {t.id for t in base} and len(base) < MAX_TRECHOS_BASE:
            base.append(achados[0])
    base = base[: MAX_TRECHOS - len(trechos_mapa)]
    disponiveis = {**trechos_mapa, **{t.id: t for t in base}}
    contexto_completo = [f"{t.titulo} {t.secao} {t.texto}" for t in disponiveis.values()]

    # 3 a 5. responder, verificar e, se preciso, repetir uma vez ----------------
    critica = ""
    for tentativa in range(1, MAX_TENTATIVAS + 1):
        mensagens = [
            {"role": "system", "content": SISTEMA_AGENTE},
            {"role": "user", "content": mensagem_usuario(pergunta_limpa, montar_contexto(list(disponiveis.values())))},
        ]
        if critica:
            mensagens.append({"role": "user", "content": critica})
        saida = llm.conversar(mensagens, esquema=esquema_rag(list(disponiveis)), max_tokens=MAX_TOKENS_AGENTE)
        resp.chamadas_llm += 1
        resp.json_valido = True
        interpretar_saida_fundamentada(resp, saida, disponiveis)
        resp.passos.append(
            {
                "passo": "responder",
                "tentativa": tentativa,
                "trechos_no_contexto": list(disponiveis),
                "resultado": resp.motivo,
            }
        )

        if resp.recusa:
            if resp.motivo == "modelo_recusou" or tentativa == MAX_TENTATIVAS:
                break  # o modelo disse que os trechos não respondem: a recusa é respeitada
            critica = (
                "VERIFICACAO: a saída anterior veio incompleta ou sem fonte. Refaça com \"analise\" e \"evidencia\" "
                "de uma frase cada, \"resposta\" de até 3 frases e os ids dos trechos usados em \"fontes\"."
            )
            continue

        # números: valem os de qualquer trecho entregue ao modelo (texto, título ou seção).
        # Número fora dos trechos é o sinal mais forte de invenção (valor, prazo, telefone).
        sem_apoio = numeros_sem_apoio(resp.resposta, contexto_completo, pergunta_limpa)
        # texto: reprova só o caso extremo, em que quase nada da resposta vem dos trechos e a
        # evidência também não confere.
        apoio = apoio_lexical(resp.resposta, contexto_completo)
        aprovado = not sem_apoio and (bool(resp.evidencia_confere) or apoio >= APOIO_MINIMO)
        resp.passos.append(
            {
                "passo": "verificar",
                "tentativa": tentativa,
                "evidencia_confere": bool(resp.evidencia_confere),
                "apoio_lexical": round(apoio, 2),
                "numeros_sem_apoio": sem_apoio,
                "aprovado": aprovado,
            }
        )
        if aprovado:
            resp.motivo = "respondido_com_fonte_verificado"
            break
        if tentativa == MAX_TENTATIVAS:
            resp.recusa, resp.resposta, resp.fontes = True, RECUSA_PADRAO, []
            resp.motivo = "reprovado_na_verificacao"
            resp.passos.append({"passo": "recusar", "motivo": resp.motivo})
            break
        problema = (
            "os números " + ", ".join(sem_apoio) + " não aparecem nos trechos"
            if sem_apoio
            else "a resposta usa informação que não está escrita nos trechos"
        )
        critica = (
            f"VERIFICACAO: a resposta anterior foi reprovada porque {problema}. "
            "Refaça usando só o que está escrito nos trechos; se eles não respondem, recuse."
        )

    return encerrar()


__all__ = ["responder_agente", "planejar", "numeros_sem_apoio", "PAPEL"]
