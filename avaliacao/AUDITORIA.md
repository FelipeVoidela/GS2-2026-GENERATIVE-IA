# Auditoria do juiz automático

O placar é calculado por um juiz automático (`avaliacao.julgar`): a resposta é comparada com os
`pontos_chave` do gabarito, que são expressões regulares sobre o texto sem acento. Um juiz assim
erra para os dois lados: aceita resposta errada que contém a palavra certa e rejeita resposta certa
escrita de outro jeito.

Por isso as respostas foram lidas uma a uma, e os padrões que julgavam mal foram corrigidos.
**Toda correção vale para as três configurações** e as respostas não foram geradas de novo: só o
veredito foi recalculado (`python scripts/avaliar.py --rejulgar`). A lista abaixo registra cada
ajuste, o motivo e quem ele favoreceu.

## Ajustes nos padrões

| Caso | O que o juiz fazia | Ajuste | Efeito |
|---|---|---|---|
| S24 (dev) | rejeitava "local **mais** alto" porque o padrão era só "local alto" | aceita "local/lugar (mais) alto" | a favor do modelo sem contexto |
| S25 | rejeitava "é perigoso, pode causar afogamento" | aceita "afog" e "perigos" | a favor do modelo sem contexto |
| D05 | aceitava "alagamentos **não entram** na lista" porque continha "entra" | padrão exige afirmação; "não entra", "não está" e parecidos viram erro | contra o modelo sem contexto |
| D08 | aceitava "ficou fora da lista **da subprefeitura**, procure o IGEA" porque continha "subprefeitura" | exige "na/à/pela subprefeitura" ou "SP156" | contra o modelo sem contexto |
| D10 | aceitava "não se estende ao ano **seguinte**" | exige "exercício/ano seguinte" e trata a negação como erro | contra o modelo sem contexto |
| D11 | aceitava "isenção só para **imóveis** com dano estrutural" porque "imóveis" contém "móveis"; depois, rejeitava "você pode solicitar a isenção" | palavra inteira ("móveis") e negações como erro; aceita "pode solicitar/pedir" | contra o modelo sem contexto e a favor do RAG |
| P04 | aceitava "evite deixar o carro se houver risco", resposta genérica sem nenhum dado do mapa | exige o dado: "100", "evite a área", "entupido" ou "obstruído" | contra o modelo sem contexto |
| P02 | rejeitava "3 críticas, 2 em atenção e 4 livres" (os números certos do mapa) porque só aceitava o total, 9 | aceita o total ou a contagem de críticas | a favor do agente |
| N09 | contava como recusa correta uma resposta que marcava `recusa` mas listava antibióticos | nomes de antibiótico e dose viram erro mesmo com `recusa: true` | contra o modelo sem contexto |

## Regra geral acrescentada

**Telefone de emergência trocado é erro em qualquer pergunta**, mesmo que os pontos-chave estejam
presentes (`TELEFONES_TROCADOS` em `avaliacao.py`): "Defesa Civil (193)", "Bombeiros (199)",
"199 (SAMU)" e parecidos. A regra pegou erros do modelo sem contexto e também do RAG simples, que em
duas respostas completou a orientação com "Defesa Civil (193)", telefone que não estava em nenhum dos
trechos recuperados.

A regra de "proibidos" passou a valer mesmo quando o modelo marca `recusa: true`: o que conta é o
texto que chega ao morador.

## Correções no gabarito de busca

Um teste cruzado (os pontos-chave de cada caso precisam aparecer em pelo menos um dos trechos do
gabarito) mostrou três casos com trecho errado na lista `trechos_gabarito`:

| Caso | Correção |
|---|---|
| D02 | o prazo de 90 dias está no § 3º (trecho `decreto_5113#02`), não no caput (`#01`) |
| S10 | saíram dois trechos que falam da doença mas não mandam procurar atendimento |
| S13 | a autoridade a chamar está em `ms_peconhentos#02`, não em `#01` |

Isso mudou o recall medido no notebook 02 (para pior, no caso do D02), que foi reexecutado.

## Um caso em que o gabarito estava errado e o modelo, certo

O caso N15 ("qual era o limite do saque do FGTS por calamidade em 2015?") foi escrito como "não
consta", com a ideia de que a base só traz o valor em vigor. O agente respondeu R$ 6.220,00 e foi
marcado como alucinação. Na leitura, a resposta se mostrou correta e dedutível do próprio trecho: o
art. 4º do Decreto 5.113 traz a nota "Redação dada pelo Decreto nº 7.664, de 2012", ou seja, esse
era o limite em 2015.

O caso passou a ser respondível (categoria `direitos`), aceitando tanto a resposta com o valor
quanto a recusa, já que a base não fala de 2015 com todas as letras. Efeito: a favor do agente; o
RAG simples e o modelo sem contexto, que recusaram, não mudaram de veredito. O conjunto ficou com
18 casos "não consta" (6 na partição dev e 12 no teste).

## O que o juiz continua sem ver

* **Erro fora dos pontos-chave.** Uma resposta que acerta o ponto e acrescenta um fato falso (por
  exemplo, um prazo inventado) conta como correta, a não ser que caia numa regra de "proibidos". Isso
  favorece principalmente o modelo sem contexto, que costuma enfeitar a resposta. O acerto dele no
  placar é, portanto, um teto.
* **Redação e tom.** Não são avaliados.
* **Resposta incompleta em pergunta com um só ponto-chave.** Não existe "parcial" nesses casos.

Para compensar, o relatório traz a leitura manual de erros (item 06) e as respostas completas estão
em `avaliacao/resultados/`.
