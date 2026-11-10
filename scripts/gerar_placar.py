"""Gera avaliacao/placar.csv e avaliacao/placar.md a partir das respostas gravadas.

    python scripts/gerar_placar.py

Não chama a LLM: só lê avaliacao/resultados/ (ver scripts/avaliar.py).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from boca_de_lobo_ia import avaliacao, config  # noqa: E402

COLUNAS = [
    ("configuracao", "Configuração"),
    ("casos", "Casos"),
    ("acerto_pct", "Acerto (%)"),
    ("alucinacao_pct", "Alucinação (%)"),
    ("recusa_indevida_pct", "Recusa indevida (%)"),
    ("recusa_no_nao_consta_pct", 'Recusa no "não consta" (%)'),
    ("citacao_correta_pct", "Citação correta (%)"),
    ("json_valido_pct", "JSON válido (%)"),
    ("tempo_mediano_s", "Tempo mediano (s)"),
    ("chamadas_llm_media", "Chamadas à LLM"),
]

LEGENDA = """
**Como ler**

* **Acerto**: respostas corretas (todos os pontos-chave do gabarito) mais recusas corretas, sobre o total de casos.
* **Alucinação**: o sistema entregou ao morador uma resposta errada, ou respondeu algo que não consta na base, sobre o total de casos.
* **Recusa indevida**: recusou uma pergunta que a base (ou o mapa) responde, sobre os casos respondíveis.
* **Recusa no "não consta"**: recusou quando devia, sobre os casos que não constam.
* **Citação correta**: entre as respostas dadas a casos respondíveis, quantas citam um trecho do gabarito. O modelo sem contexto cita de memória, sem trecho conferível, por isso fica em 0.
* Respostas parciais (acertou só parte dos pontos) não contam como acerto nem como alucinação.
"""


def formatar(v) -> str:
    if v is None:
        return "n/d"
    if isinstance(v, float):
        return f"{v:.1f}".replace(".", ",")
    return str(v)


def tabela_md(linhas: list[dict], colunas=COLUNAS) -> str:
    cab = "| " + " | ".join(rotulo for _, rotulo in colunas) + " |"
    sep = "|" + "|".join("---" for _ in colunas) + "|"
    corpo = ["| " + " | ".join(formatar(linha.get(chave)) for chave, _ in colunas) + " |" for linha in linhas]
    return "\n".join([cab, sep, *corpo])


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    teste = avaliacao.placar("teste")
    if not teste:
        sys.exit("Não há respostas gravadas em avaliacao/resultados/. Rode scripts/avaliar.py.")
    sem_produto = avaliacao.placar("teste", excluir_categorias=("produto",))
    dev = avaliacao.placar("dev")
    categorias = avaliacao.placar_por_categoria("teste")

    with (config.DIR_AVALIACAO / "placar.csv").open("w", encoding="utf-8", newline="") as f:
        campos = ["recorte"] + [c for c, _ in COLUNAS] + ["corretas_respondiveis_pct", "parciais_pct", "evidencia_confere_pct", "tempo_medio_s"]
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        for recorte, linhas in [("teste", teste), ("teste_sem_produto", sem_produto), ("dev", dev)]:
            for linha in linhas:
                w.writerow({"recorte": recorte, **linha})

    cat_cols = [("configuracao", "Configuração"), ("categoria", "Categoria"), ("casos", "Casos"),
                ("acerto_pct", "Acerto (%)"), ("alucinacao_pct", "Alucinação (%)")]
    md = [
        "# Placar das três configurações",
        "",
        f"Modelo: `{config.MODELO_LLM}` (local, CPU) · embeddings: `{config.MODELO_EMBEDDING}` · "
        f"limiar de recusa: {config.limiar_recusa()} · temperatura 0, semente 42.",
        "",
        "Gerado por `scripts/gerar_placar.py` a partir de `avaliacao/resultados/`.",
        "",
        f"## Partição de teste ({teste[0]['casos']} casos): o placar que vale",
        "",
        tabela_md(teste),
        LEGENDA,
        f"## Teste sem a categoria `produto` ({sem_produto[0]['casos']} casos)",
        "",
        "As 5 perguntas de `produto` pedem a situação atual de um bairro, que só a ferramenta do mapa responde. "
        "Sem elas, a comparação entre RAG simples e agente fica restrita ao que os dois podem responder.",
        "",
        tabela_md(sem_produto),
        "",
        "## Teste por categoria",
        "",
        tabela_md(categorias, cat_cols),
        "",
        f"## Partição dev ({dev[0]['casos']} casos), usada para calibrar",
        "",
        tabela_md(dev),
        "",
    ]
    (config.DIR_AVALIACAO / "placar.md").write_text("\n".join(md), encoding="utf-8")
    print(tabela_md(teste))
    print("\ngravados: avaliacao/placar.md e avaliacao/placar.csv")


if __name__ == "__main__":
    main()
