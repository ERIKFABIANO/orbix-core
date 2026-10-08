# O que muda no front

Lista tudo que o front precisa ganhar ou ajustar para funcionar com o back-end real (`orbix-core`). Complementa o `docs/API_CONTRACT.md` do repositório do front: o que está lá continua valendo, exceto onde este documento diz o contrário.

**Estado em 04/10/2026: tudo que está listado abaixo foi implementado** direto no `orbix-declare` (commit a partir de `ff73498`), já que o Filipe não pôde mexer no front nesta rodada. Fica registrado aqui como referência do que mudou e por quê; o checklist no fim está marcado como feito.

**Estado do back-end em 03/10/2026:** todas as rotas descritas aqui existem e têm teste. Login com carteira, e-mail e GitHub estão ligados em produção (`https://api-declare.orbixlab.com.br`), e o e-mail real já sai pelo Resend (`no-reply@orbixlab.com.br`). Google segue desligado até a chave existir (`GET /api/auth/providers` informa).

## 0. Novidades de 03/10 (depois do commit `ff73498` do front)

O `docs/FRONTEND_RELEASE_9_12.md` do front foi escrito olhando um commit antigo do back-end. Hoje **todas as rotas que ele lista como faltando existem**, com os campos novos. Abaixo, o que mudou e o que o front precisa para usar.

### 0.1 Finalizar o relatório sem gerar o DeCripto

`POST /api/report/:month/finalize` (com token, sem corpo)

Resposta `200`: o mesmo `ReportDetail` do `GET /api/report/:month`, já com `status: "final"`. Congela o CSV, calcula o hash e manda gravar a atestação na Solana. Chamar de novo num relatório já final só devolve o detalhe.

Erros: os mesmos de antes (`409 month_open`, `409 missing_prices`, `409 nothing_to_report`, `503 storage_unavailable`).

**Por que importa:** a tela do relatório hoje só habilita "Gerar DeCripto" quando o relatório já é final, mas não havia como finalizar pela interface. Sugestão: um botão **"Finalizar relatório"** no rascunho, com a confirmação de que o relatório não muda mais depois. `POST /decripto` continua finalizando sozinho se ainda for rascunho.

### 0.2 Revisão de preço auditada já funciona na API

`PUT /api/events/:id/price` aceita o corpo que o front já manda:

```json
{ "unitPriceBrl": 0.85, "reason": "Preço da corretora no dia", "evidence": "Print da Binance 23/09", "confirmed": true }
```

- Ou manda os três (`reason`, `evidence`, `confirmed: true`) ou nenhum. Faltar algum devolve `422 review_incomplete`.
- A resposta é o `TaxEvent` atualizado, com `reviewHistory` preenchido.
- O formulário de revisão **pode sair do modo demonstração** (`config.useMocks`).

### 0.3 Rota nova: informar o custo de aquisição

Para vendas cuja compra não está no histórico lido (`costUnknown: true`, item `acquisition_cost` em `review.reviewItems`).

`PUT /api/events/:id/cost`

```json
{ "costBrl": 320, "reason": "Comprei na Binance em 2025", "evidence": "Extrato da corretora", "confirmed": true }
```

- `:id` é o `id` do item de revisão (que é o mesmo `id` da linha do relatório).
- `costBrl` é o **custo total** da quantidade vendida, em reais, `>= 0`.
- Os quatro campos são obrigatórios. Corpo incompleto ou `confirmed` diferente de `true` dá `422`.
- Evento que não é venda, ou de outro usuário, dá `404`.
- Resposta: o `TaxEvent` atualizado. O ganho é recalculado, o item some de `reviewItems` e a linha do relatório passa a vir com `costUnknown: false` e `costManual: true`.

Sugestão de interface: no item de revisão, um formulário igual ao de preço, com o campo "Quanto você pagou por essa quantidade (R$)".

```ts
reviewAcquisitionCost: (id: string, body: { costBrl: number; reason: string; evidence: string; confirmed: true }) =>
  http<TaxEvent>("PUT", `/api/events/${enc(id)}/cost`, body),
```

### 0.4 Campos novos

`TaxEvent` (todos opcionais, podem vir `null`):

| Campo | O que é |
|---|---|
| `wallet` | `{ address, label }` da carteira do evento |
| `protocol` | Ex.: `Jupiter`, `Raydium`, `Hyperliquid` |
| `unitPriceBrl`, `priceProvider`, `priceObservedAt` | Preço unitário usado e de onde veio |
| `ptax`, `ptaxDate` | PTAX de venda usada e a data dela |
| `ruleVersion` | Versão das regras de cálculo (`br-2026.1`) |
| `feesBrl`, `costBrl`, `gainBrl` | Taxas, custo e ganho da venda |
| `pendingReasons` | Por que o evento ainda precisa de revisão |
| `reviewHistory` | Revisões feitas pelo usuário (veja abaixo) |

`EventReview` ganhou `kind: "price" | "cost"`, `previousCostBrl` e `newCostBrl`. Em revisão de custo, os campos de preço podem ser ignorados.

`ReportRow` ganhou `costManual: boolean`: o custo daquela venda foi informado pelo usuário. Vale um selo "custo informado".

`Dashboard` ganhou `exemptionStatus: "exempt" | "taxable" | null` (isenção de R$ 35 mil nas vendas à vista do mês).

`ReportDetail.review`:

```ts
interface ReportReview {
  engineVersion: string | null;
  coverage: { state: "complete" | "partial" | "unknown"; importedFrom: string | null; importedThrough: string | null; importedEvents: number | null };
  limitations: string[];          // textos prontos para mostrar
  pendingReasons: string[];
  unsupportedOperations: string[];
  reviewItems: { id: string; kind: "acquisition_cost" | "classification"; label: string }[];
  decriptoReady: boolean;
}
```

`coverage.state` vem `partial` quando a leitura de alguma carteira bateu no limite de transações ou falhou. O motivo vem em `limitations`.

### 0.5 `decriptoReady` fica `false` por enquanto

Ele só vira `true` quando o arquivo seguir o leiaute oficial da Receita, que ainda não implementamos. Com a regra atual do front, o botão "Gerar DeCripto" vai ficar sempre desabilitado. Duas opções:

1. Manter assim e deixar só "Finalizar relatório" e o CSV (recomendado, é o honesto).
2. Liberar o botão com o texto "Resumo para preenchimento", sem prometer arquivo oficial.

**Decisão tomada em 04/10:** opção 1. O botão fica desabilitado com um texto explicando o motivo (`title`); "Finalizar relatório" é a ação principal do rascunho, e é ela que grava o hash na Solana.

### 0.6 Mensagem de login da carteira

A primeira linha da mensagem SIWS usa o domínio do `Origin` da requisição **se ele estiver na lista do CORS**. Senão, usa `declare.orbixlab.com.br`. A mensagem também ganhou a linha `Chain ID: mainnet`. Nada muda no front: ele já assina a mensagem que recebe.

## Resumo

| # | Mudança | Tamanho |
|---|---|---|
| 1 | Login por e-mail com código, Google e GitHub, além da carteira | Grande: 2 telas novas e a tela de login |
| 2 | `User.address` pode ser `null`; campos novos em `User` | Médio: tipos e 3 lugares que mostram o endereço |
| 3 | Fluxo depois do login para quem não tem carteira | Pequeno |
| 4 | Adicionar carteira Solana colando o endereço | Pequeno |
| 5 | Regra de quando um relatório vira "final" | Pequeno: textos e um erro novo |
| 6 | Códigos de erro novos | Pequeno |
| 7 | Atualizar `mock.ts` para o modo demonstração continuar igual à API | Médio |
| 8 | Textos novos e política de privacidade citando o e-mail | Pequeno |
| 9 | Campo novo `costUnknown` nas linhas do relatório | Pequeno |
| 10 | Link do explorer da atestação em devnet | Pequeno |
| 11 | Decisões pendentes | 3 respostas |

Nenhuma variável de ambiente nova no front.

## 1. Novas formas de login

Hoje só existe login com carteira. Passam a existir quatro formas, todas devolvendo o mesmo `Session`:

| Forma | Como funciona |
|---|---|
| Carteira Solana | Igual ao contrato atual (`/api/auth/nonce` e `/api/auth/verify`). Nada muda |
| E-mail | A pessoa digita o e-mail, recebe um código de 6 dígitos e digita o código. **Sem senha**: cadastro e login são o mesmo fluxo |
| Google | Botão que leva ao Google e volta logado |
| GitHub | Botão que leva ao GitHub e volta logado |

### 1.1 Saber quais formas estão ligadas

`GET /api/auth/providers` (sem token)

```json
{ "wallet": true, "email": true, "google": false, "github": false }
```

Uma forma fica `false` enquanto a chave dela não estiver configurada no servidor. **Esconda o botão quando vier `false`**, em vez de mostrar um botão que dá erro.

### 1.2 E-mail com código

**Passo 1:** `POST /api/auth/email/start`

```json
{ "email": "ana@exemplo.com" }
```

Resposta `200`:

```json
{ "sent": true, "expiresAt": "2026-10-01T18:10:00Z", "resendAfter": 60 }
```

- A resposta é a mesma para e-mail novo e e-mail já cadastrado. Não existe "e-mail não encontrado".
- `resendAfter`: segundos até poder pedir outro código. Mostre um contador no botão "Reenviar código".
- O código vale 10 minutos e aceita 5 tentativas.

**Passo 2:** `POST /api/auth/email/verify`

```json
{ "email": "ana@exemplo.com", "code": "482913" }
```

Resposta `200`: `Session` (o mesmo objeto do login com carteira).

Erros:

| Status | `code` | Quando |
|---|---|---|
| 422 | `invalid_email` | E-mail com formato inválido |
| 401 | `invalid_code` | Código errado |
| 401 | `code_expired` | Código vencido ou já usado. Peça outro |
| 429 | `too_many_attempts` | 5 erros seguidos. O código foi invalidado; peça outro |
| 429 | `rate_limited` | Pedidos de código demais. A mensagem diz quanto esperar |
| 503 | `email_unavailable` | Serviço de e-mail fora do ar |

**Tela nova: código de verificação.** Seis campos de dígito (ou um campo com `inputmode="numeric"` e `autocomplete="one-time-code"`), o e-mail mostrado acima, botão "Reenviar código" com contador e link "Usar outro e-mail". Colar o código inteiro precisa funcionar.

### 1.3 Google e GitHub

**Passo 1:** `GET /api/auth/oauth/google/start?next=/painel` (ou `github`)

`next` é opcional e precisa ser um caminho interno começando com `/`. Resposta `200`:

```json
{ "url": "https://accounts.google.com/o/oauth2/v2/auth?…" }
```

O front faz `window.location.href = url`.

**Passo 2:** a pessoa autoriza no Google ou GitHub. O provedor devolve para o back-end, que redireciona o navegador para:

```
https://declare.orbixlab.com.br/auth/callback#code=<código de uso único>&next=/painel
```

Em caso de falha:

```
https://declare.orbixlab.com.br/auth/callback#error=oauth_failed
```

Os dados vêm depois do `#` de propósito: o que fica no fragmento não é enviado a servidores nem aparece em logs.

**Passo 3:** `POST /api/auth/exchange`

```json
{ "code": "<código recebido no fragmento>" }
```

Resposta `200`: `Session`. O código vale 60 segundos e só funciona uma vez.

**Página nova: `/auth/callback`.** Componente cliente que:

1. Lê `window.location.hash`.
2. Se tiver `error`, vai para `/login` mostrando a mensagem correspondente.
3. Se tiver `code`, chama `api.exchange(code)`, depois `signIn(session)`.
4. Limpa o fragmento com `history.replaceState` e navega para `next` (validando com o mesmo `safeNext` do login) ou segue a regra da seção 3.

Erros que podem chegar em `#error=` ou na troca:

| `code` | Significado |
|---|---|
| `oauth_failed` | O provedor recusou ou a pessoa cancelou |
| `oauth_state` | A tentativa expirou (mais de 10 minutos) ou foi adulterada. Tente de novo |
| `email_not_verified` | A conta do provedor não tem e-mail confirmado |
| `provider_unavailable` | Forma de login desligada no servidor |
| `code_expired` | O código do fragmento venceu ou já foi usado |

### 1.4 Contas com o mesmo e-mail viram uma só

Se a pessoa entra com Google usando `ana@exemplo.com` e depois com código por e-mail no mesmo endereço, é a mesma conta. Isso só vale para e-mail confirmado pelo provedor. Login com carteira não tem e-mail, então é uma conta separada até a pessoa vincular (seção 1.5).

### 1.5 Vincular outra forma de login a uma conta existente

Usa as mesmas rotas, mas **enviando o `Authorization: Bearer`** da sessão atual. O back-end entende que é vínculo, não login novo.

| Para vincular | Chamadas, com o token |
|---|---|
| Carteira | `GET /api/auth/nonce` e `POST /api/auth/verify` |
| E-mail | `POST /api/auth/email/start` e `POST /api/auth/email/verify` |
| Google ou GitHub | `GET /api/auth/oauth/:provider/start` e o fluxo normal |

Erro novo: `409 identity_in_use` quando aquela carteira, e-mail ou conta já pertence a outra pessoa.

Isso pode ficar para depois do beta. O único caso necessário agora é o da seção 3.

### 1.6 Tela de login

Sugestão de ordem, mantendo a carteira como destaque (é o que o pitch promete):

1. Botões das carteiras detectadas (como hoje).
2. Separador "ou".
3. Campo de e-mail com botão "Receber código".
4. Botões "Continuar com Google" e "Continuar com GitHub".

## 2. Mudanças em `User` e nos tipos

```ts
export type Plan = "free" | "pro" | "accountant";            // "accountant" é novo
export type LoginMethod = "wallet" | "email" | "google" | "github";

export interface User {
  id: string;
  address: string | null;        // ANTES: string. null para quem entrou sem carteira
  email: string | null;          // novo
  displayName: string | null;    // novo. Vem do Google/GitHub quando existe
  loginMethods: LoginMethod[];   // novo
  hasWallets: boolean;           // novo. false = nenhuma carteira cadastrada
  plan: Plan;
  agentQuestionsLeft: number;
  onboarded: boolean;
}
```

Em `src/lib/api/index.ts`, funções novas:

```ts
providers: () => http<Record<LoginMethod, boolean>>("GET", "/api/auth/providers"),
emailStart: (email: string) => http<EmailStartResponse>("POST", "/api/auth/email/start", { email }),
emailVerify: (email: string, code: string) => http<Session>("POST", "/api/auth/email/verify", { email, code }),
oauthStart: (provider: "google" | "github", next?: string) =>
  http<{ url: string }>("GET", `/api/auth/oauth/${provider}/start${next ? `?next=${enc(next)}` : ""}`),
exchange: (code: string) => http<Session>("POST", "/api/auth/exchange", { code }),
```

**Lugares que hoje assumem que `user.address` existe:**

| Arquivo | Linha | O que fazer |
|---|---|---|
| `src/components/app-shell.tsx` | 101 e 102 | Mostrar `displayName`, depois `email`, depois o endereço encurtado |
| `src/app/sincronizacao/sync-view.tsx` | 82 | Mesma regra |
| `src/app/(app)/configuracoes/page.tsx` | bloco da conta | Mostrar e-mail e as formas de login (`loginMethods`) |

## 3. Depois do login: quem não tem carteira

Hoje: `onboarded ? next : "/sincronizacao"`. Quem entra por e-mail, Google ou GitHub chega sem nenhuma carteira, e a sincronização não tem o que ler.

Regra nova, na ordem:

1. `!user.hasWallets` → `/carteiras`, com um estado de boas-vindas ("Adicione sua primeira carteira para começar").
2. `!user.onboarded` → `/sincronizacao`.
3. Caso contrário → `next` ou `/painel`.

Depois de adicionar a primeira carteira, chame `refreshUser()` e mande para `/sincronizacao`.

## 4. Adicionar carteira Solana colando o endereço

`POST /api/wallets` já aceitava `network: "solana"` no back-end, mas o formulário do front só valida Hyperliquid. Passa a permitir as duas redes:

| Rede | Validação no front |
|---|---|
| `solana` | Base58, de 32 a 44 caracteres: `^[1-9A-HJ-NP-Za-km-z]{32,44}$` |
| `hyperliquid` | `^0x[0-9a-fA-F]{40}$` (como hoje) |

O back-end valida de novo. Carteira adicionada assim vem com `verifiedAt: null`: os dados são públicos e ficam só para leitura, mas a posse não foi provada. A carteira usada no login vem com `verifiedAt` preenchido e `isLogin: true`.

Sugestão de interface: um selo "não verificada" discreto. O botão "Verificar" (que usa o fluxo da seção 1.5) pode ficar para depois.

## 5. Quando um relatório vira "final"

O contrato não dizia o que finaliza um relatório. A regra do back-end:

| Ação | Mês já terminou | Mês em andamento |
|---|---|---|
| `GET /api/report/:month` | Mostra `draft` até alguém finalizar | Sempre `draft` |
| `GET /api/report/:month/csv` | Se final, devolve o arquivo congelado. Se rascunho, gera um CSV provisório | CSV provisório |
| `POST /api/report/:month/decripto` | **Finaliza o relatório**: congela o CSV, calcula o hash, manda gravar na Solana e devolve o arquivo | `409 month_open` |

Consequências para a tela:

- "Gerar DeCripto" é o botão que finaliza. Vale um texto de confirmação: depois de finalizado, o relatório não muda mais, mesmo que a pessoa corrija um preço.
- Depois de finalizar, `status` passa a `final` e `attestation` fica `null` por alguns segundos, até a transação confirmar. O texto `pendingNote` já cobre isso. Recarregue o relatório a cada 5 segundos enquanto `status === "final" && attestation === null`.
- Em rascunho, `DownloadLink.url` do CSV é uma URL `data:` (o arquivo vem embutido, nada é guardado no servidor). O `triggerDownload` atual funciona com ela. Se o front tiver uma Content Security Policy restritiva, confira se `data:` passa.
- O "mês terminou" é calculado no fuso de Brasília.

Erros novos nessas rotas:

| Status | `code` | Quando |
|---|---|---|
| 409 | `month_open` | Tentou finalizar um mês que ainda não terminou |
| 409 | `missing_prices` | Há eventos sem preço no mês. A pessoa precisa informar o preço manual antes de finalizar |
| 409 | `nothing_to_report` | O mês não tem nenhuma alienação |
| 503 | `storage_unavailable` | Falha ao guardar o arquivo |

### O CSV oficial tem colunas e uma linha a mais

Cabeçalho do arquivo gerado pelo back-end (as quatro últimas colunas entraram em 07/10, sempre no fim):

```
data,tipo,ativo,quantidade,ptax,valor_brl,custo_brl,ganho_brl,preco_manual,taxas_brl,custo_desconhecido,rede,carteira
```

- `taxas_brl`: taxa da operação em reais, informativa. Vazia quando a fonte não informa.
- `custo_desconhecido`: `sim` quando a compra não está no histórico lido (custo zero, ganho inflado até o usuário informar o custo).
- `rede` e `carteira` (endereço encurtado): de onde veio cada linha.

O rascunho termina na linha `total`; a linha `rascunho,sim` saiu (quem marca o provisório é o nome do arquivo). O relatório final tem uma última linha depois do total:

```
verificacao,7f3c…(64 caracteres hex),,,,,,,,,,,
```

`TaxEvent` ganhou, em 07/10:

- `fillCount`: quantos fills da corretora formam o evento.
- `quantityIn` e `quantityInAsset`: o outro lado da troca (o que entrou). `null` fora de swap.
- `positionBeforeQty` e `avgCostUnitBrl`: posição do ativo antes da venda e custo médio por unidade usado. `costBrl` = `quantity` × `avgCostUnitBrl`.
- `unitPriceBrl` passou a sair do valor antes do arredondamento para centavos (funding em USDC = PTAX da linha).

Em 08/10:

- `TaxEvent.costUnknown` e `TaxEvent.costManual`: as mesmas marcações da linha do relatório, direto no evento.
- `TaxEvent.direction` (`"in"` ou `"out"`) e `TaxEvent.counterparty` nas transferências. Saída de cripto passou a aparecer como `transfer` (antes só a entrada aparecia). `counterparty` é `null` quando a transação tem mais de um endereço do outro lado, e nas transferências lidas antes de 08/10 até a carteira Solana ser sincronizada de novo.
- CSV oficial: coluna `custo_informado` no fim (14 colunas). `sim` quando o usuário digitou o custo de aquisição da venda.
- `POST /api/agent` não devolve mais `503 agent_unavailable` nem `429 quota`: responde `200` com `message.source = "rules"` e `rulesReason` (`"unavailable"` ou `"quota"`). O texto é montado pelas regras de cálculo, sem IA, e não gasta a cota. Com IA, `source = "ai"` e `rulesReason = null`. O front deve rotular a resposta como "Regras, não IA" quando `source = "rules"`.
- CORS: `http://127.0.0.1:3000` liberado, além de `localhost:3000` e `localhost:3100`. Outras portas caem no domínio de produção e a carteira recusa a assinatura.

É um código aleatório que impede alguém de descobrir os valores do relatório testando combinações contra o hash público. A página `/v/[id]` não muda: ela calcula o SHA-256 do arquivo inteiro, e essa linha faz parte do arquivo.

O `reportToCsv` do front continua só para o modo demonstração.

### Sobre o arquivo DeCripto

O arquivo devolvido por "Gerar DeCripto" é, por enquanto, um resumo estruturado para apoiar o preenchimento. **Ele não segue o leiaute oficial da Receita**, porque ainda não temos o manual do leiaute em mãos. Sugiro que o texto do botão e da tela não prometam "arquivo pronto para enviar" até isso ser resolvido.

## 6. Códigos de erro novos

Formato igual ao do contrato: `{ "error": { "code": "…", "message": "…" } }`, com `message` no idioma do `Accept-Language`. O front pode mostrar `message` direto. A lista serve para os casos em que a tela precisa reagir de forma diferente:

| `code` | Status | Reação sugerida |
|---|---|---|
| `invalid_code`, `code_expired`, `too_many_attempts` | 401, 401, 429 | Na tela do código |
| `rate_limited` | 429 | Mostrar a mensagem; desabilitar o botão por alguns segundos |
| `provider_unavailable`, `email_unavailable` | 503 | Mostrar a mensagem |
| `oauth_failed`, `oauth_state`, `email_not_verified` | 400 | Voltar ao login com a mensagem |
| `identity_in_use` | 409 | No vínculo de conta |
| `month_open`, `missing_prices`, `nothing_to_report` | 409 | Na tela do relatório |
| `agent_unavailable` | 503 | Na tela do agente |
| `sync_in_progress` | 409 | Ao remover uma carteira que está sincronizando |

Um detalhe do cliente HTTP atual: `401` com sessão ativa encerra a sessão. No vínculo de e-mail (seção 1.5) um código errado devolve `401 invalid_code` com a sessão ativa. Para isso não derrubar o usuário, o cliente precisa encerrar a sessão só quando `error.code === "unauthorized"`.

## 7. Modo demonstração (`mock.ts`)

Para o demo continuar igual à API:

- `providers()` devolvendo os quatro como `true`.
- `emailStart` e `emailVerify`: aceitar qualquer e-mail e o código `000000`.
- `oauthStart`: devolver uma URL interna que cai em `/auth/callback#code=demo`.
- `exchange("demo")` devolvendo a sessão de demonstração.
- `User` do demo com os campos novos (`email`, `displayName`, `loginMethods`, `hasWallets`).
- Um segundo usuário de demo sem carteira, para testar o fluxo da seção 3.
- `generateDecripto` devolvendo `409 month_open` no mês corrente.

## 8. Textos e privacidade

- Chaves novas em `messages/pt.ts` e `messages/en.ts` para: campo de e-mail, tela do código, botões do Google e GitHub, erros acima, boas-vindas sem carteira, selo "não verificada", confirmação de finalização.
- O e-mail que o usuário recebe (assunto, texto e visual) é montado pelo back-end, em português ou inglês conforme o `Accept-Language` da chamada `email/start`.
- A promessa "sem senha, sem e-mail" da tela de login e do pitch muda: o produto passa a guardar e-mail de quem escolher essa forma. A política de privacidade e o texto de exclusão de conta precisam citar isso. Continua valendo "sem senha" e "nunca pedimos chave privada".

## 9. Campo novo nas linhas do relatório: `costUnknown`

`ReportRow` ganhou um campo opcional:

```ts
export interface ReportRow {
  // …campos atuais
  manualPrice?: boolean;
  /** true quando o ativo vendido entrou antes do histórico lido: o custo de aquisição foi tratado como zero. */
  costUnknown?: boolean;
}
```

Quando vem `true`, o ganho daquela linha está superestimado, porque o back-end não achou a compra. Acontece com carteira antiga ou com ativo recebido antes do período lido. Sugestão: um ícone de aviso na linha, com o texto "Não encontramos a compra deste ativo no histórico lido. O custo foi considerado zero." Sem tratamento no front, nada quebra: o campo é só ignorado.

## 10. Link do explorer da atestação

Até 04/10 o hash do relatório é gravado na **devnet** da Solana, e os eventos são da **mainnet**. O front monta o link da transação de atestação com `NEXT_PUBLIC_SOLANA_CLUSTER`, que hoje é `mainnet-beta`: o link da atestação abriria no explorer da rede errada.

Enquanto a atestação estiver em devnet, o link dela precisa de `?cluster=devnet`, e os links dos eventos continuam sem o parâmetro. Como os eventos já chegam com `explorerUrl` pronto do back-end, basta o front usar o cluster configurado só no link da atestação e definir `NEXT_PUBLIC_SOLANA_CLUSTER=devnet` até a troca. Aviso quando a gravação for para a mainnet.

## 11. Decisões pendentes

1. **Onde guardar o token.** Hoje é `localStorage`. Com Google e GitHub o risco não muda, mas a alternativa com cookie `httpOnly` continua de pé. Ela pede `credentials: "include"` no cliente HTTP e o back-end passa a setar o cookie na resposta do login e da troca. Continua em aberto: ficou em `localStorage` por agora.
2. **Ordem e destaque na tela de login.** A sugestão da seção 1.6 foi seguida: carteira em destaque, depois e-mail, depois Google/GitHub.
3. **Botão "Verificar carteira"** (seção 4): ficou para depois do beta. Carteira colada por endereço continua `verifiedAt: null` e nunca é a carteira de login.

## 12. Botão de demonstração sempre visível (04/10)

Pedido explícito: o botão "Entrar no modo demonstração" fica visível mesmo com `NEXT_PUBLIC_API_URL` apontando para a API real, para quem for avaliar o projeto (os jurados do hackathon) poder testar sem carteira nem conta.

Implementação: `demoLogin()` sempre usa o `mockApi`, mas antes disso, com a API real configurada, só essa chamada ia parar no mock — todas as outras (`dashboard`, `events`, `report`...) continuavam indo pro back-end real com um token que ele não reconhece. Correção em `src/lib/api/index.ts`: `api` virou um `Proxy` que escolhe `mockApi` ou o back-end real por chamada, olhando `isDemoSession()` (`src/lib/api/client.ts`) — verdadeiro quando o token da sessão atual começa com `"demo."`, que é como o `mockApi.verify` sempre assina o token. Então uma sessão de demonstração fica inteira no mock, mesmo em produção, e uma sessão normal nunca passa por ele.

Os lugares que escondiam dado fictício só em `config.useMocks` (`DemoNotice`, o aviso de hash fictício no relatório, o link do explorer desabilitado) passaram a checar `config.useMocks || isDemoSession()`, senão uma sessão de demonstração em produção mostraria um link do Solana Explorer para uma transação que não existe.

`mock.ts` ganhou, além das rotas novas (`providers`, `emailStart/Verify`, `oauthStart/exchange`, `reviewAcquisitionCost`, `finalizeReport`): uma segunda persona sem carteira (entrar por e-mail devolve um usuário com `hasWallets: false`, para exercitar o fluxo da seção 3) e uma venda de exemplo com `costUnknown` (o swap "JUP → SOL") que começa com custo zero e vira `costManual` depois que alguém resolve pelo formulário — o mesmo item que o back-end real geraria em `review.reviewItems`.

## Checklist

- [x] Tipos atualizados (`User`, `Plan`, `LoginMethod`) e funções novas em `api`
- [x] Tela de login com e-mail, Google e GitHub, escondendo o que vier `false` em `providers`
- [x] Tela do código de verificação
- [x] Página `/auth/callback`
- [x] `user.address` nulo tratado nos 3 lugares
- [x] Regra de redirecionamento depois do login
- [x] Formulário de carteira aceitando Solana
- [x] Confirmação antes de "Gerar DeCripto" e atualização automática até a atestação aparecer
- [x] Cliente HTTP encerrando a sessão só com `code === "unauthorized"`
- [x] `mock.ts` atualizado
- [x] Aviso de `costUnknown` nas linhas do relatório
- [x] Link da atestação com o cluster certo
- [x] Textos em PT e EN; política de privacidade citando o e-mail
- [x] Botão "Finalizar relatório" usando `POST /api/report/:month/finalize`
- [x] Formulário de revisão de preço fora do modo demonstração
- [x] Formulário de custo de aquisição (`PUT /api/events/:id/cost`) nos itens `acquisition_cost`
- [x] Selo "custo informado" (`costManual`) e histórico com `kind: "cost"`
- [x] Decidir o texto do botão DeCripto enquanto `decriptoReady` for `false`
