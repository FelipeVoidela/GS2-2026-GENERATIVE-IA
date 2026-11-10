"""Ferramenta do agente: situação dos bueiros inspecionados em um bairro ou rua.

É a integração com o restante do produto. O componente Dados (Big Data) publica a
view ``ouro_vw_mapa_cidadao`` como JSON; o contrato é esse arquivo, o mesmo que o
mapa do cidadão lê. Não há dado pessoal nele (ver LGPD no repositório de Dados).

Se a URL estiver fora do ar, usa a cópia local em ``dados/mapa_cidadao.json`` e
avisa na resposta de que data são os dados (degradação com transparência).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import requests

from . import config
from .busca import normalizar
from .trechos import Trecho

_CACHE: dict = {"em": 0.0, "dados": None, "origem": ""}
VALIDADE_CACHE_S = 600
URL_MAPA_PUBLICO = "https://felipevoidela.github.io/GS2-2026-Big-Data-Architecture/"
ROTULO_ALERTA = {"EVITE A AREA": "Evite a área", "ATENCAO": "Atenção", "NORMAL": "Normal"}


@dataclass
class Consulta:
    local: str
    encontrados: int
    texto: str
    origem: str  # "url" ou "copia_local"
    gerado_em: str


def carregar_mapa() -> tuple[dict, str]:
    """Devolve (json do mapa, origem). Cache de 10 minutos."""
    agora = time.time()
    if _CACHE["dados"] is not None and agora - _CACHE["em"] < VALIDADE_CACHE_S:
        return _CACHE["dados"], _CACHE["origem"]
    dados, origem = None, ""
    if config.MAPA_URL:
        try:
            r = requests.get(config.MAPA_URL, timeout=6)
            r.raise_for_status()
            dados, origem = r.json(), "url"
        except (requests.RequestException, ValueError):
            dados = None
    if dados is None:
        dados = json.loads(config.ARQ_MAPA_LOCAL.read_text(encoding="utf-8"))
        origem = "copia_local"
    _CACHE.update(em=agora, dados=dados, origem=origem)
    return dados, origem


TIPOS_DE_VIA = {"rua", "r", "avenida", "av", "travessa", "praca", "estrada", "alameda", "viela", "largo"}


def _palavras(texto: str) -> list[str]:
    return normalizar(str(texto or "")).replace("-", " ").replace(".", " ").split()


def filtrar(local: str, pontos: list[dict]) -> list[dict]:
    """Pontos do local pedido.

    Se o morador escreveu um tipo de via ("Rua ..."), procura primeiro pelo logradouro;
    senão, pelo distrito, depois pela subprefeitura e só então pelo logradouro. Em cada
    campo vale primeiro o nome exato e depois o nome que contém todas as palavras pedidas
    (para "Vila Mariana" não casar com "Vila Maria").
    """
    alvo = _palavras(local)
    if not alvo or len("".join(alvo)) < 3:
        return []
    campos = ("distrito", "subprefeitura", "logradouro")
    if alvo[0] in TIPOS_DE_VIA:
        campos = ("logradouro", "distrito", "subprefeitura")
    for campo in campos:
        exatos = [p for p in pontos if _palavras(p.get(campo)) == alvo]
        if exatos:
            return exatos
        contidos = [p for p in pontos if set(alvo) <= set(_palavras(p.get(campo)))]
        if contidos:
            return contidos
    return []


def consultar(local: str) -> Consulta:
    """Resumo em texto, pronto para entrar no contexto da LLM como um trecho."""
    mapa, origem = carregar_mapa()
    gerado = str(mapa.get("gerado_em", ""))[:16]
    pontos = filtrar(local, mapa.get("pontos", []))
    if not pontos:
        texto = (
            f"O mapa do Boca de Lobo (dados de {gerado}) não tem nenhuma boca de lobo inspecionada "
            f"em \"{local}\". O piloto cobre só parte da cidade, então não é possível dizer como está esse local."
        )
        return Consulta(local, 0, texto, origem, gerado)
    por_status = {s: sum(1 for p in pontos if p["status"] == s) for s in ("critico", "atencao", "livre")}
    por_alerta = {a: sum(1 for p in pontos if p["nivel_alerta"] == a) for a in ROTULO_ALERTA}
    chuva = max(p.get("chuva_prevista_24h_mm") or 0 for p in pontos)
    prob = max(p.get("prob_chuva_max_pct") or 0 for p in pontos)
    inspecao = max(str(p.get("data_ultima_inspecao") or "") for p in pontos)
    distritos = sorted({p["distrito"].title() for p in pontos})
    linhas = [
        f"Dados do mapa do Boca de Lobo gerados em {gerado} (última inspeção do robô: {inspecao}).",
        f"Local consultado: {local}. Distritos encontrados: {', '.join(distritos[:4])}.",
        f"Bocas de lobo inspecionadas: {len(pontos)}. Entupidas (críticas): {por_status['critico']}; "
        f"em atenção (parcialmente obstruídas): {por_status['atencao']}; livres: {por_status['livre']}.",
        f"Alerta ao morador: {por_alerta['EVITE A AREA']} ponto(s) com aviso \"Evite a área\", "
        f"{por_alerta['ATENCAO']} com \"Atenção\" e {por_alerta['NORMAL']} com \"Normal\".",
        f"Chuva prevista para as próximas 24 horas: até {chuva:.1f} mm (probabilidade máxima de {prob:.0f}%). "
        f"O aviso \"Evite a área\" aparece quando o bueiro está entupido, fica em área de risco e a chuva "
        f"prevista passa de {mapa.get('limiar_chuva_mm', 10):.0f} mm.",
    ]
    piores = sorted(pontos, key=lambda p: -(p.get("score_prioridade") or 0))[:4]
    piores = [p for p in piores if p["nivel_alerta"] != "NORMAL"]
    if piores:
        itens = [
            f"{p['logradouro']} ({ROTULO_ALERTA[p['nivel_alerta']]}, {p['obstrucao_pct']:.0f}% obstruído)"
            for p in piores
        ]
        linhas.append("Pontos que mais preocupam: " + "; ".join(itens) + ".")
    return Consulta(local, len(pontos), " ".join(linhas), origem, gerado)


def como_trecho(c: Consulta) -> Trecho:
    """Embala a consulta como um trecho citável (id começa com 'mapa#')."""
    chave = normalizar(c.local).replace(" ", "_")[:40] or "local"
    return Trecho(
        id=f"mapa#{chave}",
        fonte_id="mapa",
        titulo="Mapa do cidadão: situação das bocas de lobo inspecionadas",
        secao=c.local,
        texto=c.texto,
        orgao="Boca de Lobo (componente Dados, camada Ouro)",
        url=URL_MAPA_PUBLICO,
        licenca="Dado do próprio produto (view ouro_vw_mapa_cidadao, sem dado pessoal)",
        tipo_texto="dado",
        palavras=len(c.texto.split()),
    )
