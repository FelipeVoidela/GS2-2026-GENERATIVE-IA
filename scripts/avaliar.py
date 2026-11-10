"""Roda as três configurações sobre os casos de teste e grava as respostas.

    python scripts/avaliar.py                      # tudo (dev e teste), as 3 configurações
    python scripts/avaliar.py --particao dev       # só a partição de calibração
    python scripts/avaliar.py --config rag agente  # só algumas configurações
    python scripts/avaliar.py --rejulgar           # reaplica o gabarito às respostas já gravadas

A rodada pode ser interrompida e retomada: respostas já gravadas em
avaliacao/resultados/ não são refeitas. Para refazer do zero, apague a pasta.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# A avaliação usa a cópia local do mapa (dados/mapa_cidadao.json), para o placar não
# mudar quando o componente Dados publicar um export novo.
os.environ["MAPA_URL"] = ""

from boca_de_lobo_ia import avaliacao, busca, config  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--particao", choices=["dev", "teste"], help="padrão: as duas")
    p.add_argument("--config", nargs="+", choices=avaliacao.CONFIGURACOES, default=avaliacao.CONFIGURACOES)
    p.add_argument("--rejulgar", action="store_true")
    args = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    casos = avaliacao.carregar_casos(args.particao)
    if args.rejulgar:
        for nome in args.config:
            print(nome, "rejulgados:", len(avaliacao.rejulgar(nome, avaliacao.carregar_casos())))
        return

    indice = busca.Indice()
    problemas = avaliacao.conferir_gabarito(avaliacao.carregar_casos(), indice)
    if problemas:
        sys.exit("Gabarito inconsistente:\n" + "\n".join(problemas))
    limiar = config.limiar_recusa()
    print(f"{len(casos)} casos | modelo {config.MODELO_LLM} | limiar {limiar}")

    def progresso(nome: str, r: dict) -> None:
        j = r["julgamento"]
        print(f"[{nome:12}] {r['id']} {j['veredito']:16} {r['tempo_s']:6.1f}s  {r['resposta'][:90]!r}", flush=True)

    for nome in args.config:
        avaliacao.executar(nome, casos, indice, limiar, ao_concluir=progresso)

    for particao in ([args.particao] if args.particao else ["dev", "teste"]):
        print(f"\nPlacar ({particao}):")
        for linha in avaliacao.placar(particao):
            print("  ", linha)


if __name__ == "__main__":
    main()
