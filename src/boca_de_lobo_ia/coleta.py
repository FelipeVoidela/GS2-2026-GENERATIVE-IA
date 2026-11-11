"""Coleta das fontes da base de conhecimento.

Para cada fonte de ``base/fontes.json``:

* ``tipo_texto = "literal"``: baixa o original (HTML ou PDF), extrai o texto útil e
  grava ``base/documentos/<id>.md``. Só entram como texto literal atos oficiais
  (leis e decretos, que não têm proteção autoral) e páginas com licença que
  permite a reprodução com citação da fonte.
* ``tipo_texto = "ficha"``: o documento em ``base/documentos`` é uma ficha-resumo
  escrita pela equipe, com link para o original. A coleta só confere se a página
  continua no ar e guarda uma cópia local (fora do Git) para conferência.

Os originais baixados ficam em ``base/bruto/`` (ignorado pelo Git).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from . import config

AGENTE = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
BLOCOS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "td", "th", "blockquote", "dt", "dd"}
TITULOS = {"h1", "h2", "h3", "h4", "h5"}
DESCARTAR = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form", "button", "iframe"}


@dataclass
class Bloco:
    tipo: str  # "h" (título de seção), "p" (parágrafo) ou "li" (item de lista)
    texto: str


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def carregar_fontes() -> list[dict]:
    return json.loads(config.ARQ_FONTES.read_text(encoding="utf-8"))


def _url_segura(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, urllib.parse.quote(p.path, safe="/%"), p.query, ""))


def baixar(fonte: dict, atualizar: bool = False) -> Path:
    """Baixa o original da fonte para base/bruto/ (usa o cache se já existir)."""
    config.DIR_BRUTO.mkdir(parents=True, exist_ok=True)
    ext = "pdf" if fonte["formato"].upper() == "PDF" else "html"
    destino = config.DIR_BRUTO / f"{fonte['id']}.{ext}"
    if destino.exists() and not atualizar:
        return destino
    r = requests.get(
        _url_segura(fonte["url"]),
        headers={"User-Agent": AGENTE, "Accept-Language": "pt-BR,pt;q=0.9"},
        timeout=60,
    )
    r.raise_for_status()
    if ext == "pdf" and not r.content.startswith(b"%PDF"):
        raise ValueError(f"{fonte['id']}: a URL não devolveu um PDF (login ou redirecionamento?)")
    destino.write_bytes(r.content)
    return destino


# --------------------------------------------------------------------------- #
# extração
# --------------------------------------------------------------------------- #
def _limpar(texto: str) -> str:
    texto = texto.replace("\xa0", " ").replace("​", "").replace("﻿", "")
    texto = texto.replace("ﬁ", "fi").replace("ﬂ", "fl")
    texto = re.sub(r"\s+", " ", texto).strip()
    texto = re.sub(r"\s+([,.;:!?)])", r"\1", texto)  # espaço sobrando antes de pontuação
    return re.sub(r"\(\s+", "(", texto)


def _sopa(dados: bytes, codificacao: str | None) -> BeautifulSoup:
    if codificacao:
        return BeautifulSoup(dados.decode(codificacao, errors="replace"), "lxml")
    return BeautifulSoup(dados, "lxml")


def _blocos_html(raiz: Tag) -> list[Bloco]:
    """Percorre o HTML em ordem e devolve títulos, parágrafos e itens de lista."""
    for t in raiz.find_all(DESCARTAR):
        t.decompose()
    # texto revogado em leis: o Planalto risca com <strike> ou com estilo line-through
    for t in raiz.find_all(["strike", "s", "del"]):
        t.decompose()
    for t in raiz.find_all(style=re.compile(r"line-through")):
        t.decompose()
    blocos: list[Bloco] = []
    estruturais = BLOCOS | {"div", "ul", "ol", "table", "section", "article", "tr", "tbody"}

    def tem_bloco_filho(el: Tag) -> bool:
        return any(isinstance(f, Tag) and f.name in estruturais for f in el.descendants)

    def visitar(el: Tag) -> None:
        solto: list[str] = []  # texto "em linha" direto no contêiner (fora de <p>/<li>)

        def fechar_solto() -> None:
            txt = _limpar(" ".join(solto))
            solto.clear()
            if txt:
                blocos.append(Bloco("p", txt))

        for filho in el.children:
            if isinstance(filho, Comment):
                continue
            if isinstance(filho, NavigableString):
                solto.append(str(filho))
                continue
            if not isinstance(filho, Tag) or filho.name == "br":
                continue
            if tem_bloco_filho(filho):
                fechar_solto()
                visitar(filho)
            elif filho.name in estruturais:
                fechar_solto()
                txt = _limpar(filho.get_text(" ", strip=True))
                if txt:
                    tipo = "h" if filho.name in TITULOS else ("li" if filho.name == "li" else "p")
                    blocos.append(Bloco(tipo, txt))
            elif "toggle" in (filho.get("class") or []):
                # pergunta de FAQ em sanfona (portal gov.br): vira título da seção
                fechar_solto()
                txt = _limpar(filho.get_text(" ", strip=True))
                if txt:
                    blocos.append(Bloco("h", txt))
            else:
                solto.append(filho.get_text(" ", strip=True))
        fechar_solto()

    visitar(raiz)
    return [b for b in blocos if len(b.texto) > 1]


def _cortar(blocos: list[Bloco], inicio: str | None, fins: list[str]) -> list[Bloco]:
    if inicio:
        for i, b in enumerate(blocos):
            if b.texto.startswith(inicio):
                blocos = blocos[i:]
                break
    for i, b in enumerate(blocos):
        if any(b.texto.startswith(f) for f in fins):
            blocos = blocos[:i]
            break
    return blocos


def extrair_html(dados: bytes, regra: dict) -> list[Bloco]:
    sopa = _sopa(dados, regra.get("codificacao"))
    raiz = sopa.select_one(regra.get("seletor", "body")) or sopa.body
    blocos = _blocos_html(raiz)
    blocos = _cortar(blocos, regra.get("inicio"), regra.get("fim", []))
    # itens de menu/rodapé curtíssimos que sobram no início não ajudam a busca
    ignorar = set(regra.get("ignorar", []))
    return [b for b in blocos if b.texto not in ignorar]


RE_ARTIGO = re.compile(r"^Art\.?\s*(\d+)\s*[ºo°]?(-[A-Z])?", re.I)


def _normalizar_lei(texto: str) -> str:
    """Conserta o 'o' sobrescrito que o HTML do Planalto separa do número."""
    texto = re.sub(r"(\bArt\.|\bart\.|\barts\.|§§|§)\s*(\d+)\s*o\b", r"\1 \2º", texto)
    texto = re.sub(r"\bn\s*o\s+(?=\d)", "nº ", texto)
    return re.sub(r"(\d)º\s*-\s*([A-Z])\b", r"\1º-\2", texto)


def extrair_lei(dados: bytes, regra: dict) -> list[Bloco]:
    """Leis e decretos: um título de seção por artigo; parágrafos e incisos embaixo."""
    sopa = _sopa(dados, regra.get("codificacao"))
    blocos = _cortar(_blocos_html(sopa.body), regra.get("inicio"), regra.get("fim", []))
    manter = set(regra.get("artigos", []))
    saida: list[Bloco] = []
    numero_atual: int | None = None
    for b in blocos:
        b = Bloco(b.tipo, _normalizar_lei(b.texto))
        m = RE_ARTIGO.match(b.texto)
        if m:
            numero_atual = int(m.group(1))
            if not manter or numero_atual in manter:
                ordinal = "º" if numero_atual < 10 else ""
                saida.append(Bloco("h", f"Art. {numero_atual}{ordinal}{m.group(2) or ''}"))
        if numero_atual is None or (manter and numero_atual not in manter):
            continue
        # títulos de capítulo/seção e notas de redação não são conteúdo do artigo
        if re.match(r"^(CAP[ÍI]TULO|Se[çc][ãa]o|T[ÍI]TULO)\b", b.texto) or b.texto.isupper():
            continue
        if re.match(r"^\((Vide|Regulamento|Vig[êe]ncia|Produ[çc][ãa]o de efeito)", b.texto):
            continue
        if re.fullmatch(r"[.\s“”\"]+", b.texto):  # linhas pontilhadas de leis alteradoras
            continue
        # nome de capítulo ou seção que sobrou entre dois artigos
        if len(b.texto.split()) <= 5 and not re.match(r"^(Art|§|Par|[IVXL]+\b|[a-z]\))", b.texto):
            continue
        saida.append(Bloco("p", b.texto))
    return saida


def extrair_pdf(dados: bytes, regra: dict) -> list[Bloco]:
    from pypdf import PdfReader

    leitor = PdfReader(io.BytesIO(dados))
    secoes = set(regra.get("secoes", []))
    blocos: list[Bloco] = []
    paragrafo: list[str] = []

    def fechar() -> None:
        if paragrafo:
            blocos.append(Bloco("p", _limpar(" ".join(paragrafo))))
            paragrafo.clear()

    for pagina in leitor.pages:
        for linha in (pagina.extract_text() or "").splitlines():
            linha = _limpar(linha)
            if not linha:
                fechar()
                continue
            if linha in secoes:
                fechar()
                blocos.append(Bloco("h", linha))
                continue
            paragrafo.append(linha)
            if linha.endswith((".", ":", ";", "!", "?")):
                fechar()
        fechar()
    return _cortar(blocos, regra.get("inicio"), regra.get("fim", []))


EXTRATORES = {"html": extrair_html, "lei": extrair_lei, "pdf": extrair_pdf}


# --------------------------------------------------------------------------- #
# escrita dos documentos
# --------------------------------------------------------------------------- #
def cabecalho(fonte: dict, acessado_em: str) -> str:
    campos = ["id", "titulo", "orgao", "url", "formato", "licenca", "tipo_texto"]
    linhas = ["---"] + [f"{c}: {fonte[c]}" for c in campos] + [f"acessado_em: {acessado_em}", "---", ""]
    return "\n".join(linhas)


def para_markdown(fonte: dict, blocos: list[Bloco], acessado_em: str) -> str:
    linhas = [cabecalho(fonte, acessado_em), f"# {fonte['titulo']}", ""]
    anterior = ""
    for b in blocos:
        if b.tipo == "h":
            linhas += ["", f"## {b.texto}", ""]
        elif b.tipo == "li":
            linhas.append(f"- {b.texto}")
        else:
            linhas += ([""] if anterior == "li" else []) + [b.texto, ""]
        anterior = b.tipo
    texto = "\n".join(linhas)
    return re.sub(r"\n{3,}", "\n\n", texto).strip() + "\n"


def coletar(atualizar: bool = False, somente: list[str] | None = None) -> list[dict]:
    """Baixa as fontes e (re)gera os documentos literais. Devolve um resumo por fonte."""
    config.DIR_DOCUMENTOS.mkdir(parents=True, exist_ok=True)
    resumo = []
    for fonte in carregar_fontes():
        if somente and fonte["id"] not in somente:
            continue
        item = {"id": fonte["id"], "tipo_texto": fonte["tipo_texto"], "situacao": "ok", "blocos": None}
        try:
            caminho = baixar(fonte, atualizar=atualizar)
            dados = caminho.read_bytes()
            item["sha256"] = hashlib.sha256(dados).hexdigest()[:16]
            if fonte["tipo_texto"] == "literal":
                regra = fonte["extracao"]
                blocos = EXTRATORES[regra["tipo"]](dados, regra)
                if not blocos:
                    raise ValueError("a extração não devolveu texto; confira as regras em fontes.json")
                destino = config.DIR_DOCUMENTOS / f"{fonte['id']}.md"
                # data de acesso = dia em que o original foi baixado (não o dia desta execução)
                baixado_em = dt.date.fromtimestamp(caminho.stat().st_mtime).isoformat()
                destino.write_text(para_markdown(fonte, blocos, baixado_em), encoding="utf-8")
                item["blocos"] = len(blocos)
            else:
                ficha = config.DIR_DOCUMENTOS / f"{fonte['id']}.md"
                if not ficha.exists():
                    item["situacao"] = "ficha ausente em base/documentos"
        except Exception as erro:  # noqa: BLE001 - o resumo mostra a falha por fonte
            item["situacao"] = f"falha: {type(erro).__name__}: {erro}"
        resumo.append(item)
    return resumo
