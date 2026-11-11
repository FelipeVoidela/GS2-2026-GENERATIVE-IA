"""Mede o tempo de resposta das três configurações, uma pergunta por vez, sem nada concorrendo.

    python scripts/medir_tempos.py

Os tempos gravados em avaliacao/resultados/ não servem para comparar custo: parte das rodadas
foi feita com duas avaliações disputando o mesmo processador. Este script roda 12 perguntas da
partição dev em sequência e grava avaliacao/tempos.json. Leva cerca de 15 minutos em CPU.
"""
from __future__ import annotations

import json
import os
import platform
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ["MAPA_URL"] = ""

from boca_de_lobo_ia import agente, avaliacao, busca, config, geracao  # noqa: E402

AMOSTRA = ["S01", "S03", "S06", "S10", "S18", "S21", "D01", "D06", "D12", "M01", "N01", "N05"]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    casos = {c["id"]: c for c in avaliacao.carregar_casos("dev")}
    indice = busca.Indice()
    limiar = config.limiar_recusa()
    funcoes = {
        "sem_contexto": lambda p: geracao.responder_sem_contexto(p),
        "rag": lambda p: geracao.responder_rag(p, indice, limiar=limiar),
        "agente": lambda p: agente.responder_agente(p, indice, limiar=limiar),
    }
    geracao.responder_sem_contexto("Qual é o telefone da Defesa Civil?")  # aquece o modelo
    saida = {
        "modelo": config.MODELO_LLM,
        "maquina": f"{platform.processor() or platform.machine()} (CPU, sem GPU)",
        "amostra": AMOSTRA,
        "configuracoes": {},
    }
    for nome, funcao in funcoes.items():
        tempos, chamadas = [], []
        for cid in AMOSTRA:
            r = funcao(casos[cid]["pergunta"])
            tempos.append(r.tempo_s)
            chamadas.append(r.chamadas_llm)
            print(f"[{nome:12}] {cid} {r.tempo_s:6.1f}s  {r.chamadas_llm} chamada(s)", flush=True)
        tempos_ordenados = sorted(tempos)
        saida["configuracoes"][nome] = {
            "perguntas": len(tempos),
            "tempo_mediano_s": round(statistics.median(tempos), 1),
            "tempo_medio_s": round(statistics.mean(tempos), 1),
            "tempo_p90_s": round(tempos_ordenados[int(0.9 * (len(tempos) - 1))], 1),
            "tempo_maximo_s": round(max(tempos), 1),
            "chamadas_llm_media": round(statistics.mean(chamadas), 2),
            "tempos_s": tempos,
        }
    destino = config.DIR_AVALIACAO / "tempos.json"
    destino.write_text(json.dumps(saida, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for nome, m in saida["configuracoes"].items():
        print(nome, {k: v for k, v in m.items() if k != "tempos_s"})
    print("gravado:", destino)


if __name__ == "__main__":
    main()
