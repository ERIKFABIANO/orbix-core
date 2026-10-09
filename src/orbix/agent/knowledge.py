"""Base de conhecimento do agente: o que ele sabe de regra fiscal brasileira para criptoativos.

Entra no prompt de sistema. É texto, não treino: para refinar o agente, edite este arquivo.

Como manter:
- cada bloco diz de onde veio; ao mudar uma regra, atualize a fonte e a data em `REVIEWED_AT`;
- escreva o que a norma diz, não opinião. O que é interpretação vai em PONTOS SEM DEFINIÇÃO;
- o agente explica a conta do Orbix e a regra geral. Decisão é do contribuinte e do contador.

Fontes consultadas em 09/10/2026:
- Instrução Normativa RFB nº 2.291/2025 (DeCripto), arts. 3º, 5º, 6º, 11, 12, 13 e 18;
- Perguntas e Respostas IRPF 2026 da Receita Federal, perguntas 473, 474, 575, 653 e 679;
- Lei nº 14.754/2023 e Instrução Normativa RFB nº 2.180/2024 (aplicações financeiras no exterior);
- Medida Provisória nº 1.303/2025: perdeu a validade em outubro de 2025 sem ser votada.
"""

REVIEWED_AT = "09/10/2026"

KNOWLEDGE = """\
BASE DE CONHECIMENTO (regras gerais no Brasil, pessoa física; revisada em {reviewed_at})

1. Natureza e regime
- Criptoativo é bem. O lucro na venda é ganho de capital, apurado pelo próprio contribuinte.
- Onde o ativo está define o regime. Em carteira própria (autocustódia), vale a residência \
do contribuinte: residente no Brasil, ativo no Brasil (Perguntas e Respostas IRPF 2026, \
pergunta 474). É o caso das carteiras que o Orbix lê (Solana e Hyperliquid, sem corretora).
- Ativo custodiado ou negociado por instituição no exterior (corretora estrangeira) segue \
outra regra: aplicação financeira no exterior, 15% na declaração anual, sem isenção mensal \
(Lei 14.754/2023). O Orbix não calcula esse regime.

2. Ganho de capital (regime das carteiras lidas)
- Ganho = valor da alienação menos custo de aquisição. Custo pelo custo médio do ativo.
- Trocar um criptoativo por outro é alienação do que saiu, mesmo sem passar por real \
(pergunta 653). Vale também entre stablecoins e de stablecoin para outro ativo.
- Isenção: se o total alienado no mês, somando todos os criptoativos, for de até R$ 35.000, \
o ganho do mês é isento. Passou de R$ 35.000, todo o ganho do mês é tributado, não só o excesso.
- Alíquotas sobre o ganho: 15% até R$ 5 milhões; 17,5% de R$ 5 a 10 milhões; 20% de R$ 10 a \
30 milhões; 22,5% acima de R$ 30 milhões. O Orbix estima sempre com 15%.
- Pagamento: DARF código 4600, até o último dia útil do mês seguinte ao da venda. A apuração \
oficial é feita no programa GCAP da Receita. O Orbix não gera DARF nem GCAP.
- A isenção continua valendo em 2026: a medida provisória que acabaria com ela (MP \
1.303/2025, 17,5% sem isenção) perdeu a validade em outubro de 2025.

3. Conversão para reais
- Valor em moeda estrangeira vai para dólar e depois para reais pela cotação de venda do \
dólar do Banco Central (PTAX de fechamento) da data da operação (IN 2.291/2025, art. 11; \
pergunta 473). Sem cotação no dia (fim de semana, feriado), usa-se a do último dia útil anterior.

4. DeCripto (Instrução Normativa RFB 2.291/2025)
- É a declaração de operações com criptoativos que substituiu a da IN 1.888/2019. Entrega \
pelo sistema Coleta Nacional, no e-CAC.
- Pessoa física residente no Brasil entrega quando opera por corretora no exterior, por \
plataforma descentralizada ou sem intermediário, e o total das operações do mês passa de \
R$ 35.000 (art. 5º). Esse limite soma as operações do mês (compras, vendas, permutas, \
transferências), não só as vendas: é diferente do limite da isenção do imposto.
- O que informar (art. 6º): compra, venda, permuta, transferências recebidas (inclusive \
airdrop, recompensa de staking, mineração) e enviadas, entre outras.
- Prazo: até o último dia útil do mês seguinte ao das operações (art. 12). A entrega mensal \
pelo usuário é obrigatória a partir das operações de julho de 2026.
- Multas para pessoa física (art. 13): R$ 100 por mês ou fração de atraso; 1,5% do valor da \
operação omitida ou informada com erro; metade se corrigir antes de a Receita agir.
- O Orbix prepara os dados (operações, valores em reais, PTAX), mas ainda não gera o arquivo \
no leiaute oficial. O envio é do contribuinte.

5. Declaração anual (IRPF)
- Ficha Bens e Direitos, grupo 08: código 01 Bitcoin, 02 outras criptomoedas, 03 stablecoins, \
10 NFTs, 99 outros. Obrigatório para cada tipo com custo de aquisição de R$ 5.000 ou mais.
- Informa-se o custo de aquisição, nunca o valor de mercado.
- Ganho isento (mês até R$ 35.000) vai em Rendimentos Isentos; ganho tributado vem do GCAP.

6. Pontos sem definição clara da Receita (diga que é ponto em aberto e recomende contador)
- Contratos perpétuos e funding em plataforma descentralizada: não há orientação específica. \
O Orbix adota o critério conservador: resultado realizado menos taxas, tributado a 15%, fora \
da isenção mensal.
- Airdrop e recompensa de staking: a Receita não definiu o momento nem o valor da tributação. \
O Orbix registra a entrada (airdrop com custo zero) e tributa na venda.
- Compensar prejuízo de um mês com ganho de outro: a regra do ganho de capital não prevê essa \
compensação. O Orbix soma ganhos e perdas dentro do mesmo mês e não leva prejuízo adiante.
- Token trazido de outra rede pelo próprio usuário (ponte): não é compra. O custo é o que a \
pessoa pagou originalmente; o Orbix pede que ela informe.
"""


def knowledge() -> str:
    return KNOWLEDGE.format(reviewed_at=REVIEWED_AT)
