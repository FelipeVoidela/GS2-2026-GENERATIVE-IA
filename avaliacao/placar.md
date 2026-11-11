# Placar das três configurações

Modelo: `gemma3:4b` (local, CPU) · embeddings: `bge-m3` · limiar de recusa: 0.55 · temperatura 0, semente 42.

Gerado por `scripts/gerar_placar.py` a partir de `avaliacao/resultados/`.

## Partição de teste (56 casos): o placar que vale

| Configuração | Casos | Acerto (%) | Alucinação (%) | Recusa indevida (%) | Recusa no "não consta" (%) | Citação correta (%) | JSON válido (%) | Chamadas à LLM |
|---|---|---|---|---|---|---|---|---|
| 1. LLM sem contexto | 56 | 30,4 | 60,7 | 4,5 | 33,3 | 0,0 | 100,0 | 1 |
| 2. RAG simples | 56 | 76,8 | 5,4 | 22,7 | 100,0 | 90,6 | 100,0 | 0,7 |
| 3. Agente (especialização) | 56 | 85,7 | 7,1 | 6,8 | 91,7 | 85,0 | 100,0 | 1,8 |

**Como ler**

* **Acerto**: respostas corretas (todos os pontos-chave do gabarito) mais recusas corretas, sobre o total de casos.
* **Alucinação**: o sistema entregou ao morador uma resposta errada, ou respondeu algo que não consta na base, sobre o total de casos.
* **Recusa indevida**: recusou uma pergunta que a base (ou o mapa) responde, sobre os casos respondíveis.
* **Recusa no "não consta"**: recusou quando devia, sobre os casos que não constam.
* **Citação correta**: entre as respostas dadas a casos respondíveis, quantas citam um trecho do gabarito. O modelo sem contexto cita de memória, sem trecho conferível, por isso fica em 0.
* Respostas parciais (acertou só parte dos pontos) não contam como acerto nem como alucinação.

## Teste sem a categoria `produto` (51 casos)

As 5 perguntas de `produto` pedem a situação atual de um bairro, que só a ferramenta do mapa responde. Sem elas, a comparação entre RAG simples e agente fica restrita ao que os dois podem responder.

| Configuração | Casos | Acerto (%) | Alucinação (%) | Recusa indevida (%) | Recusa no "não consta" (%) | Citação correta (%) | JSON válido (%) | Chamadas à LLM |
|---|---|---|---|---|---|---|---|---|
| 1. LLM sem contexto | 51 | 33,3 | 58,8 | 2,6 | 33,3 | 0,0 | 100,0 | 1 |
| 2. RAG simples | 51 | 84,3 | 5,9 | 12,8 | 100,0 | 90,6 | 100,0 | 0,8 |
| 3. Agente (especialização) | 51 | 86,3 | 5,9 | 7,7 | 91,7 | 82,9 | 100,0 | 1,8 |

## Teste por categoria

| Configuração | Categoria | Casos | Acerto (%) | Alucinação (%) |
|---|---|---|---|---|
| 1. LLM sem contexto | alertas_servicos | 7 | 14,3 | 71,4 |
| 1. LLM sem contexto | direitos | 12 | 16,7 | 75,0 |
| 1. LLM sem contexto | multi | 5 | 20,0 | 40,0 |
| 1. LLM sem contexto | nao_consta | 12 | 33,3 | 66,7 |
| 1. LLM sem contexto | orientacao | 14 | 57,1 | 42,9 |
| 1. LLM sem contexto | produto | 5 | 0,0 | 80,0 |
| 1. LLM sem contexto | seguranca | 1 | 100,0 | 0,0 |
| 2. RAG simples | alertas_servicos | 7 | 71,4 | 14,3 |
| 2. RAG simples | direitos | 12 | 91,7 | 8,3 |
| 2. RAG simples | multi | 5 | 80,0 | 20,0 |
| 2. RAG simples | nao_consta | 12 | 100,0 | 0,0 |
| 2. RAG simples | orientacao | 14 | 71,4 | 0,0 |
| 2. RAG simples | produto | 5 | 0,0 | 0,0 |
| 2. RAG simples | seguranca | 1 | 100,0 | 0,0 |
| 3. Agente (especialização) | alertas_servicos | 7 | 85,7 | 14,3 |
| 3. Agente (especialização) | direitos | 12 | 91,7 | 8,3 |
| 3. Agente (especialização) | multi | 5 | 80,0 | 0,0 |
| 3. Agente (especialização) | nao_consta | 12 | 91,7 | 8,3 |
| 3. Agente (especialização) | orientacao | 14 | 78,6 | 0,0 |
| 3. Agente (especialização) | produto | 5 | 80,0 | 20,0 |
| 3. Agente (especialização) | seguranca | 1 | 100,0 | 0,0 |

## Partição dev (21 casos), usada para calibrar

| Configuração | Casos | Acerto (%) | Alucinação (%) | Recusa indevida (%) | Recusa no "não consta" (%) | Citação correta (%) | JSON válido (%) | Chamadas à LLM |
|---|---|---|---|---|---|---|---|---|
| 1. LLM sem contexto | 21 | 28,6 | 57,1 | 13,3 | 33,3 | 0,0 | 100,0 | 1 |
| 2. RAG simples | 21 | 90,5 | 4,8 | 6,7 | 100,0 | 100,0 | 100,0 | 0,8 |
| 3. Agente (especialização) | 21 | 90,5 | 4,8 | 0,0 | 100,0 | 93,3 | 100,0 | 1,9 |

## Tempo de resposta

Medido por `scripts/medir_tempos.py`: 12 perguntas da partição dev, uma por vez, sem nada concorrendo. Máquina: AMD Ryzen 5 5600X, 6 núcleos, 32 GB de RAM (CPU, sem GPU; Ollama em container Docker).

| Configuração | Perguntas | Tempo mediano (s) | Tempo médio (s) | Percentil 90 (s) | Máximo (s) | Chamadas à LLM |
|---|---|---|---|---|---|---|
| 1. LLM sem contexto | 12 | 12,6 | 13,4 | 15,3 | 18,2 | 1 |
| 2. RAG simples | 12 | 27,1 | 26,0 | 29,7 | 29,9 | 1 |
| 3. Agente (especialização) | 12 | 36,3 | 36,8 | 42,6 | 46,0 | 2 |
