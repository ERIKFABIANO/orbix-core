# Agente de IA: fontes da base de conhecimento

Pesquisa feita em 09/10/2026 para montar a base de conhecimento do agente. O texto que o agente recebe está em `src/orbix/agent/knowledge.py`. Para refinar o agente, edite aquele arquivo e atualize esta lista.

O agente explica a conta que o Orbix fez e a regra geral. Ele não substitui contador, e o prompt manda dizer isso.

## Como ler esta lista

- **Oficial**: norma ou documento da Receita Federal. Vale como regra.
- **Apoio**: escritório, contabilidade ou imprensa. Serviu para localizar e conferir a regra, não é norma.
- Onde duas fontes de apoio discordam, o ponto foi para a seção "sem definição clara" da base, e o agente recomenda contador.

## Fontes oficiais

| Tema | Documento | Onde foi lido | O que entrou na base |
|---|---|---|---|
| DeCripto | Instrução Normativa RFB nº 2.291, de 14/11/2025 | [normaslegais.com.br](https://www.normaslegais.com.br/legislacao/instrucao-normativa-rfb-2291-2025.htm), [PDF do Diário Oficial](https://static.poder360.com.br/2025/11/INStrucao-normativa-rfb-No-2.291-DE-14-DE-NOVEMBRO-DE-2025-INStrucao-normativa-rfb-No-2.291-DE-14-DE-NOVEMBRO-DE-2025-DOU-Imprensa-Nacional.pdf) | Quem entrega (art. 5º), limite de R$ 35.000 por mês em operações (art. 5º, § 3º), operações informadas (art. 6º), conversão pela PTAX de venda (art. 11), prazo (art. 12), multas (art. 13), revogação da IN 1.888/2019 (art. 18), entrega pelo Coleta Nacional no e-CAC (art. 3º) |
| Ganho de capital, isenção, permuta, DARF | Perguntas e Respostas IRPF 2026, Receita Federal, perguntas 473, 474, 575, 653 e 679 | Resumo em [declarandobitcoin.com.br](https://www.declarandobitcoin.com.br/post/saiu-o-pergunt%C3%A3o2026) | Permuta entre criptoativos é alienação (653), isenção até R$ 35.000 alienados no mês (653 e 679), DARF código 4600 até o último dia útil do mês seguinte (653), autocustódia segue a residência do contribuinte (474), conversão pela PTAX de venda (473 e 474), ficha Bens e Direitos (473) |
| Ativos no exterior | Lei nº 14.754/2023 e Instrução Normativa RFB nº 2.180/2024 | [declarandobitcoin.com.br](https://www.declarandobitcoin.com.br/post/receita-federal-lan%C3%A7a-instru%C3%A7%C3%A3o-normativa-para-regulamentar-lei-14-754-que-afeta-os-criptoativos-no), [FAQ declarecripto](https://faq.declarecripto.com.br/informacoes-sobre-a-receita-federal/duvidas-sobre-a-nova-lei-para-criptoativos-nacional-x-internacional/autocustodia-nas-wallets-sao-consideradas-domicilio-fiscal-brasil) | Ativo custodiado ou negociado por instituição no exterior é aplicação financeira no exterior: 15% na declaração anual, sem isenção mensal. O Orbix não calcula esse regime |
| Fim da isenção que não aconteceu | Medida Provisória nº 1.303/2025 | [Câmara dos Deputados](https://www.camara.leg.br/noticias/1169245-medida-provisoria-compensa-recuo-na-cobranca-do-imposto-sobre-operacoes-financeiras), [Exame](https://exame.com/future-of-money/mp-1-303-com-rejeicao-na-camara-tributacao-de-criptos-nao-vai-subir-em-2026-veja-regras-atuais/) | A medida previa 17,5% sem isenção. Perdeu a validade em outubro de 2025 sem ser votada. A isenção de R$ 35.000 continua em 2026 |
| Cotação | PTAX de venda, Banco Central | [bcb.gov.br](https://www.bcb.gov.br/estabilidadefinanceira/historicocotacoes) | Fonte da conversão para reais; o agente já cita este endereço nas respostas |

## Fontes de apoio

| Tema | Fonte |
|---|---|
| Visão geral da DeCripto e do padrão da OCDE | [Mattos Filho](https://www.mattosfilho.com.br/unico/decripto-receita-federal-ocde/), [IBET](https://www.ibet.com.br/in-2-291-2025-mundo-cripto-prestacao-de-informacoes-operacoes/), [CEPEDA](https://cepeda.law/instrucao-normativa-rfb-no-2-291-2025-declaracao-de-criptoativos-decripto/?lang=en), [DNA Law](https://dnalaw.law/2025/11/19/in-rfb-no-2-291-2025-inaugura-novo-marco-regulatorio-para-reporte-de-criptoativos/) |
| Regras do imposto em 2026, alíquotas e faixas | [Blue Consult](https://blueconsult.com.br/imposto-criptomoedas-brasil-2026/), [Mattos Filho](https://www.mattosfilho.com.br/unico/declaracao-imposto-de-renda-criptoativos/), [Estratégia](https://www.estrategiaconcursos.com.br/blog/tributacao-criptoativos-brasil-2026/) |
| DARF e GCAP | [Blue Consult](https://blueconsult.com.br/darf-criptomoedas-como-gerar-pagar-imposto/), [Rolmy Jun Contabilidade](https://rolmyjuncontabilidade.com.br/investimentos/criptoativos-ganho-de-capital-gcap-declarar-2026/) |
| Declaração anual, códigos do grupo 08 | [Nomad](https://www.nomadglobal.com/portal/artigos/como-declarar-criptomoedas), [Renova Invest](https://renovainvest.com.br/blog/como-declarar-criptomoedas-no-irpf-descubra/) |
| Queda da MP 1.303/2025 | [Contábeis](https://www.contabeis.com.br/artigos/73399/a-queda-da-mp-1303-2025-o-que-muda-na-tributacao-de-investimentos-e-criptoativos/), [Mercado Bitcoin](https://www.mb.com.br/economia-digital/criptos/entenda-o-fim-da-mp-1303/) |
| Permuta de criptoativos, discussão acadêmica | [Revista Direito Tributário Atual, IBDT](https://revista.ibdt.org.br/index.php/RDTA/article/view/2315) |

## Pontos em que as fontes discordam ou a Receita não se pronunciou

O agente trata estes pontos como "em aberto" e recomenda contador.

| Ponto | O que foi encontrado | O que o Orbix faz |
|---|---|---|
| Autocustódia e plataformas descentralizadas | O Perguntas e Respostas 2026 (pergunta 474) diz que em carteira própria vale a residência do contribuinte. Uma fonte de apoio chama o tema de "zona cinzenta" | Trata as carteiras lidas como ativo no Brasil: ganho de capital com isenção mensal |
| Contratos perpétuos e funding em plataforma descentralizada | Nenhuma orientação específica encontrada | Resultado realizado menos taxas, 15%, fora da isenção |
| Airdrop e recompensa de staking | A Receita não definiu momento nem valor. As fontes de apoio divergem | Registra a entrada, airdrop com custo zero, tributa na venda |
| Compensação de prejuízo entre meses | Uma fonte de apoio diz que pode no regime nacional. A regra geral do ganho de capital não prevê | Soma ganhos e perdas no mesmo mês e não leva prejuízo adiante |
| Alíquotas acima de 15% | Faixas de 17,5%, 20% e 22,5% para ganhos acima de R$ 5 milhões | Estima sempre com 15% |
| Limite da DeCripto antes de julho de 2026 | Uma fonte de apoio cita R$ 30.000 da norma antiga até a troca | A base usa só a regra nova, R$ 35.000 |

## O que não foi lido

- O texto integral do Perguntas e Respostas IRPF 2026. Foi usado um resumo de terceiros com os números das perguntas. Vale conferir as perguntas 473, 474 e 653 no documento da Receita.
- O Manual de Orientação do Leiaute da DeCripto v1.01. Ele trata do arquivo oficial, que o Orbix ainda não gera.
- Soluções de consulta da Receita sobre criptoativos.

## Teste feito com o modelo real

Em 09/10/2026, dez perguntas com os dados da carteira de teste, mês de outubro de 2026, modelo `gpt-5.4`. Resultado: nenhuma referência interna, nenhum markdown e nenhuma data em formato técnico na resposta final. O agente recusou o pedido de chave privada, disse que não tinha dados de outro mês, citou a IN 2.291/2025 e a pergunta 474 nas respostas de regra, e avisou do custo desconhecido do KNTQ. Cada pergunta usou cerca de 3.100 tokens de entrada e levou de 2 a 8 segundos.
