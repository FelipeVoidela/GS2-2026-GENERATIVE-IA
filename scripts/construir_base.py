"""Reconstrói a base de conhecimento, do download ao índice.

    python scripts/construir_base.py               # usa as cópias já baixadas em base/bruto
    python scripts/construir_base.py --atualizar   # baixa tudo de novo das fontes oficiais
    python scripts/construir_base.py --sem-indice  # não chama o Ollama (só documentos e trechos)

Passos: (1) baixa as fontes de base/fontes.json; (2) regenera os documentos de texto
literal em base/documentos (as fichas-resumo não são sobrescritas); (3) divide tudo em
trechos (base/trechos.jsonl); (4) gera os embeddings (base/indice).
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from boca_de_lobo_ia import coleta, trechos  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--atualizar", action="store_true", help="baixa de novo mesmo que já exista cópia local")
    p.add_argument("--sem-indice", action="store_true", help="não gera embeddings")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    falhas = 0
    for item in coleta.coletar(atualizar=args.atualizar):
        marca = "ok " if item["situacao"] == "ok" else "ERRO"
        falhas += item["situacao"] != "ok"
        print(f"{marca} {item['id']:22} {item['tipo_texto']:8} {item['situacao']}")
    if falhas:
        sys.exit(f"{falhas} fonte(s) com problema; a base não foi regerada.")

    ts = trechos.gerar_trechos()
    trechos.gravar(ts)
    por_fonte = Counter(t.fonte_id for t in ts)
    print(f"\n{len(ts)} trechos de {len(por_fonte)} fontes em base/trechos.jsonl")

    if not args.sem_indice:
        from boca_de_lobo_ia import busca

        indice = busca.Indice(ts)
        print(f"índice vetorial: {indice.vetores.shape[0]} x {indice.vetores.shape[1]} em base/indice/")


if __name__ == "__main__":
    main()
