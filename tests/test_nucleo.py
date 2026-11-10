"""Testes rápidos do que não depende da LLM (rodam sem Ollama): pytest -q"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "src"))
os.environ["MAPA_URL"] = ""  # os testes usam a cópia local do mapa

from boca_de_lobo_ia import avaliacao, coleta, config, geracao, lgpd, mapa, trechos  # noqa: E402
from boca_de_lobo_ia.agente import numeros_sem_apoio  # noqa: E402
from boca_de_lobo_ia.busca import BM25, Indice, tokenizar  # noqa: E402


# --- base de conhecimento -------------------------------------------------
def test_toda_fonte_tem_documento_e_trechos():
    fontes = {f["id"] for f in coleta.carregar_fontes()}
    documentos = {p.stem for p in config.DIR_DOCUMENTOS.glob("*.md")}
    assert fontes == documentos
    com_trecho = {t.fonte_id for t in trechos.carregar()}
    assert fontes == com_trecho


def test_trechos_tem_fonte_e_tamanho_razoavel():
    ts = trechos.carregar()
    assert len(ts) >= 100
    assert len({t.id for t in ts}) == len(ts), "ids de trecho repetidos"
    for t in ts:
        assert t.url.startswith("http") and t.orgao and t.licenca and t.titulo
        assert 5 <= t.palavras <= trechos.MAX_PALAVRAS + 40, (t.id, t.palavras)


def test_trechos_gravados_batem_com_os_documentos():
    """Se alguém editar um documento e esquecer de regerar, este teste avisa."""
    gerados = [(t.id, t.texto) for t in trechos.gerar_trechos()]
    gravados = [(t.id, t.texto) for t in trechos.carregar()]
    assert gerados == gravados


def test_valor_revogado_do_fgts_nao_esta_na_base():
    """O Planalto mantém as redações antigas riscadas; só a vigente pode entrar."""
    texto = (config.DIR_DOCUMENTOS / "decreto_5113.md").read_text(encoding="utf-8")
    assert "6.220,00" in texto
    for antigo in ("2.600,00", "4.650,00", "5.400,00"):
        assert antigo not in texto


# --- busca ----------------------------------------------------------------
def test_tokenizar_tira_acento_plural_e_palavras_vazias():
    assert tokenizar("Os bueiros da região estão entupidos") == ["bueiro", "regiao", "entupido"]


def test_bm25_acha_o_documento_do_termo():
    docs = [tokenizar(t) for t in ["saque do FGTS por calamidade", "limpeza de boca de lobo", "água para beber"]]
    notas = BM25(docs).pontuar(tokenizar("como sacar o fgts"))
    assert notas.argmax() == 0


def test_busca_lexical_na_base_real():
    indice = Indice(usar_vetores=False)
    ids = [r.trecho.id for r in indice.buscar("valor do saque do FGTS por desastre natural", k=5, modo="bm25")]
    assert any(i.startswith("decreto_5113") for i in ids)


# --- LGPD -----------------------------------------------------------------
def test_mascara_identificadores_e_preserva_telefones_de_emergencia():
    texto, tipos = lgpd.mascarar(
        "Sou a Maria, CPF 123.456.789-09, tel (11) 98765-4321, maria@exemplo.com, CEP 05409-000. Ligo 199 ou 193?"
    )
    assert "123.456" not in texto and "98765" not in texto and "@" not in texto and "05409-000" not in texto
    assert "199" in texto and "193" in texto
    assert {"CPF", "TELEFONE", "EMAIL", "CEP"} <= set(tipos)


def test_numero_do_sms_nao_e_confundido_com_telefone():
    texto, tipos = lgpd.mascarar("mando o CEP pro 40199 e ligo no 156")
    assert texto == "mando o CEP pro 40199 e ligo no 156" and tipos == []


# --- geração: regras de segurança sobre a saída do modelo -----------------
class _Saida:
    modelo = "teste"

    def __init__(self, dados):
        self.dados = dados


def _trecho():
    return trechos.carregar()[0]


def test_resposta_sem_fonte_vira_recusa():
    t = _trecho()
    resp = geracao.Resposta("rag", "p", True, "")
    saida = _Saida({"analise": "", "trechos_respondem": True, "fontes": [], "evidencia": "", "resposta": "invento"})
    geracao.interpretar_saida_fundamentada(resp, saida, {t.id: t})
    assert resp.recusa and resp.motivo == "resposta_sem_fonte" and resp.resposta == geracao.RECUSA_PADRAO


def test_saida_invalida_vira_recusa():
    t = _trecho()
    resp = geracao.Resposta("rag", "p", True, "")
    geracao.interpretar_saida_fundamentada(resp, _Saida(None), {t.id: t})
    assert resp.recusa and not resp.json_valido


def test_resposta_com_fonte_passa_e_confere_evidencia():
    t = _trecho()
    resp = geracao.Resposta("rag", "p", True, "")
    frase = t.texto.split(".")[0]
    saida = _Saida({"analise": "ok", "trechos_respondem": True, "fontes": [t.id, "inexistente#99"],
                    "evidencia": frase, "resposta": "orientação"})
    geracao.interpretar_saida_fundamentada(resp, saida, {t.id: t})
    assert not resp.recusa and [f["id"] for f in resp.fontes] == [t.id] and resp.evidencia_confere


# --- agente: verificação de números ---------------------------------------
def test_numero_inventado_e_pego():
    trecho = "limitado à quantia correspondente a R$ 6.220,00, intervalo de doze meses"
    assert numeros_sem_apoio("Você pode sacar até R$ 6.220,00.", [trecho], "quanto?") == []
    assert numeros_sem_apoio("Você pode sacar até R$ 8.000 em 30 dias.", [trecho], "quanto?") == ["30", "8000"]
    assert numeros_sem_apoio("O limite é de R$ 20 mil.", ["limite de R$ 20.000,00"], "") == []


def test_numero_por_extenso_na_fonte_nao_e_tratado_como_inventado():
    """O decreto escreve "doze meses" e "noventa dias"; a resposta com dígitos está certa."""
    trecho = "intervalo não inferior a doze meses; admitida até noventa dias; até cinquenta mil habitantes; meia hora"
    assert numeros_sem_apoio("Espere 12 meses, peça em até 90 dias, 50.000 habitantes, 30 minutos.", [trecho], "") == []
    assert numeros_sem_apoio("O prazo é de 45 dias.", [trecho], "") == ["45"]


# --- mapa (integração com o componente Dados) ------------------------------
def test_mapa_filtra_por_distrito_sem_confundir_nomes_parecidos():
    pontos = json.loads(config.ARQ_MAPA_LOCAL.read_text(encoding="utf-8"))["pontos"]
    vila_mariana = mapa.filtrar("Vila Mariana", pontos)
    assert vila_mariana and {p["distrito"] for p in vila_mariana} == {"VILA MARIANA"}
    assert {p["distrito"] for p in mapa.filtrar("Itaquera", pontos)} == {"ITAQUERA"}
    assert mapa.filtrar("Cidade Inexistente", pontos) == []


def test_mapa_devolve_trecho_citavel():
    c = mapa.consultar("Grajaú")
    t = mapa.como_trecho(c)
    assert c.encontrados > 0 and t.id == "mapa#grajau" and "Bocas de lobo inspecionadas" in t.texto


# --- avaliação -------------------------------------------------------------
def test_gabarito_consistente():
    indice = Indice(usar_vetores=False)
    assert avaliacao.conferir_gabarito(avaliacao.carregar_casos(), indice) == []


def test_julgamento():
    caso = {"consta": True, "pontos_chave": [["6\\.?220"]], "trechos_gabarito": ["decreto_5113#07"]}
    certo = {"recusa": False, "resposta": "Até R$ 6.220,00.", "fontes": [{"id": "decreto_5113#07"}]}
    errado = {"recusa": False, "resposta": "Até R$ 3.000.", "fontes": []}
    assert avaliacao.julgar(caso, certo)["veredito"] == "correta"
    assert avaliacao.julgar(caso, certo)["citacao_correta"] is True
    assert avaliacao.julgar(caso, errado)["veredito"] == "incorreta"
    assert avaliacao.julgar(caso, {"recusa": True, "resposta": ""})["veredito"] == "recusa_indevida"
    # telefone de emergência trocado derruba a resposta, mesmo com o ponto-chave presente
    fone = {"consta": True, "pontos_chave": [["193", "199"]], "trechos_gabarito": []}
    assert avaliacao.julgar(fone, {"recusa": False, "resposta": "Ligue para a Defesa Civil (199) ou os Bombeiros (193)."})["veredito"] == "correta"
    assert avaliacao.julgar(fone, {"recusa": False, "resposta": "Ligue para a Defesa Civil (193)."})["veredito"] == "incorreta"
    fora = {"consta": False, "pontos_chave": [], "trechos_gabarito": []}
    assert avaliacao.julgar(fora, {"recusa": True, "resposta": ""})["veredito"] == "recusa_correta"
    assert avaliacao.julgar(fora, errado)["veredito"] == "alucinacao"
