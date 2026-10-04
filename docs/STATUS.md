# orbix-core: estado em 03/10/2026

Resumo do back-end do Orbix Declare para quem não acompanhou a construção. Cobre o que está no ar, o que mudou no banco, as regras de cálculo adotadas e o que ainda falta.

Nenhum segredo está neste arquivo. Senhas, chaves e o IP da droplet ficam fora do repositório; peça ao Erik.

## Em uma frase

O back-end do contrato está implementado e no ar: login (carteira, e-mail, Google, GitHub), carteiras, leitura da Solana e da Hyperliquid, preço em reais pela PTAX, motor fiscal, relatórios com arquivo no R2, verificação pública e agente. Faltam chaves e saldo para três partes funcionarem de verdade (lista em "O que falta").

## O que está no ar

| Item | Estado |
|---|---|
| API | `https://api-declare.orbixlab.com.br`, todas as rotas do contrato |
| Front | `https://declare.orbixlab.com.br` na Vercel, ainda em modo demonstração |
| Banco | Supabase (us-east-1), migração `0005` aplicada em 03/10 |
| Servidor | Droplet DigitalOcean em Nova York, 1 GB + 2 GB de swap, Docker Compose |
| Storage | Bucket `orbixdeclare` no Cloudflare R2 |

Teste de ponta a ponta contra a produção em 02/10, com conta descartável: 27 verificações, todas passaram. Cobriu login com carteira, leitura real de uma carteira Solana (25 transações) e de uma Hyperliquid (4.209 operações em 10 segundos), painel, eventos, relatório, agente, finalização com upload e download no R2 e exclusão da conta. Para repetir: `uv run python scripts/smoke_prod.py <carteira Solana pública> [carteira Hyperliquid]`.

**Atenção:** o código de 03/10 (finalizar relatório, custo de aquisição, revisão auditada, `ingestion_runs`, recotação agendada) está pronto e testado, mas ainda não foi publicado na droplet. A migração `0005` já está no banco e é compatível com o código que está no ar.

**O front já pode apontar para a API**, desde que as mudanças de `docs/FRONT_CHANGES.md` estejam feitas. Sem elas, o login com carteira e as telas atuais funcionam; e-mail, Google e GitHub precisam das telas novas.

## Formas de login

| Forma | No servidor | Depende de |
|---|---|---|
| Carteira Solana | Ligada | Nada |
| E-mail com código | Ligada (Resend, domínio `orbixlab.com.br`). Envio real feito pela API de produção em 03/10 | Telas novas no front |
| Google | Desligada | Criar o OAuth Client e informar id e secret |
| GitHub | Desligada | Criar o OAuth App e informar id e secret |

`GET /api/auth/providers` diz ao front quais estão ligadas.

O usuário é criado pelo back-end em `auth.users`, pela função `orbix_auth.create_user()`, que só o papel da API pode chamar. A chave `service_role` do Supabase não é usada em lugar nenhum. O Supabase Auth em si não participa do login.

## Rotas

| Grupo | Rotas |
|---|---|
| Login | `GET /api/auth/providers`, `GET /api/auth/nonce`, `POST /api/auth/verify`, `POST /api/auth/email/start`, `POST /api/auth/email/verify`, `GET /api/auth/oauth/:provider/start`, `GET /api/auth/oauth/:provider/callback`, `POST /api/auth/exchange`, `POST /api/auth/logout` |
| Conta | `GET /api/me`, `DELETE /api/me` |
| Carteiras | `GET /api/wallets`, `POST /api/wallets`, `DELETE /api/wallets/:id`, `POST /api/wallets/:id/sync` |
| Leitura | `POST /api/ingest`, `GET /api/ingest/status` |
| Painel | `GET /api/dashboard`, `GET /api/events`, `PUT /api/events/:id/price`, `PUT /api/events/:id/cost` |
| Relatórios | `GET /api/reports`, `GET /api/report/:month`, `GET /api/report/:month/csv`, `POST /api/report/:month/finalize`, `POST /api/report/:month/decripto` |
| Público | `GET /api/verify/:publicId`, `GET /health` |
| Agente | `POST /api/agent` |

## Banco

O schema de 01/10 (com `event_reviews`, `ingestion_runs`, `report_versions` e os triggers de proteção) foi mantido inteiro. As migrações `0003` a `0005` só acrescentam.

| Migração | O que faz |
|---|---|
| `0003` | Identidades, sessões, agente e colunas do app |
| `0004` | Permissões para `event_reviews` (evidência) e `ingestion_runs` (worker grava cada leitura) |
| `0005` | `events.cost_override_brl`: custo de aquisição informado pelo usuário, auditado pelo trigger de revisão |

**Tabelas novas**

| Tabela | Para quê |
|---|---|
| `orbix_auth.identities` | Formas de login de cada conta (carteira, e-mail, Google, GitHub) |
| `orbix_auth.sessions` | Sessões. O token é guardado só como SHA-256 |
| `public.agent_messages` | Histórico do agente, com RLS |

**Colunas novas**

| Tabela | Colunas |
|---|---|
| `profiles` | `onboarded_at`, `agent_questions_used`, `agent_period`, `locale` |
| `wallets` | `is_login`, `verified_at`, `status`, `sync_error`, `sync_read`, `sync_total`, `sync_cursor`, `sync_started_at` |
| `reports` | `public_id`, `cost_brl`, `tax_brl`, `events_count`, `finalized_at`, `slot`, `attested_at`, `attest_error`, `decripto_key` |

**Papéis**

| Papel | Quem usa | O que pode |
|---|---|---|
| `orbix_api` | API | Identidade e sessões; carteiras e relatórios com o dono explícito no SQL. Para ler dados do usuário, assume `authenticated` e o RLS filtra |
| `orbix_worker` | Worker | Lê e grava eventos, preços, câmbio, ativos e relatórios. Não apaga nada |
| `authenticated` | Usuário, via API | Leitura do que é dele. Escrita só em rótulo de carteira, nome de exibição, preço manual e custo informado de evento |
| `postgres` | Migrações | Nunca é usado pela aplicação |

**Como o back-end usa as tabelas que já existiam**

- `event_reviews`: toda alteração de preço ou de custo fica registrada pelo trigger, com motivo e autor. Na revisão feita pelo usuário, a API grava também a evidência informada. O histórico volta em `TaxEvent.reviewHistory`.
- `report_versions`: quando a transação na Solana confirma, o worker grava uma versão imutável com o hash, a chave do arquivo e a assinatura.
- `reports.r2_object_key`, `reporting_required` e `rules_version` são preenchidos na finalização. A versão das regras hoje é `br-2026.1`.
- `ingestion_runs`: cada leitura de carteira grava uma linha (`complete`, `partial` quando bate no limite de transações, `failed`). Alimenta `review.coverage` e `review.limitations` do relatório.
- `events.operation_id` e `events.review_status = 'excluded'` ainda não são usados pelo back-end.

**Regra de convivência:** mudança de banco só por migração do Alembic neste repositório. Em 01/10 uma alteração feita por fora apagou as políticas do worker; a `0003` as recriou. Tabela nova em `public` precisa de RLS ligado e de `revoke` dos acessos automáticos de `anon` na mesma migração.

## Regras de cálculo adotadas

Estão em `src/orbix/tax/engine.py`. São estimativa, não orientação fiscal. Precisam ser revistas por um contador antes de o produto prometer valores de imposto.

| Regra | O que o motor faz |
|---|---|
| Custo de aquisição | Custo médio ponderado por ativo |
| Swap | Alienação do que saiu e aquisição do que entrou, pelo valor em reais da operação |
| Transferência recebida e recompensa | Entram na posição pelo valor de mercado do dia |
| Transferência enviada, taxa de rede, staking | Não geram ganho |
| Transferência entre carteiras do mesmo usuário | Ignorada: o custo de aquisição é preservado |
| Venda sem aquisição conhecida | Custo zero (custo não comprovado) e a linha vem marcada com `costUnknown`. Stablecoin entra pelo próprio valor. O usuário pode informar o custo (`PUT /api/events/:id/cost`, com motivo e evidência); a linha passa a `costManual`. `UNKNOWN_COST_POLICY=market` troca o custo zero pelo valor da venda |
| Perpétuos | Ganho = resultado realizado menos taxas, convertido pela PTAX |
| Funding | Recebido é ganho; pago é custo |
| Isenção | Total alienado em spot no mês até R$ 35.000: ganho em spot isento. Perpétuos e funding não entram na isenção |
| Alíquota | 15% sobre o ganho tributável. Faixas progressivas acima de R$ 5 milhões não estão implementadas |
| Câmbio | PTAX de venda do dia da operação, ou do último dia útil |

**Preço em dólar, em ordem:** stablecoin vale 1; depois a outra perna do mesmo swap; depois CoinGecko. Na leitura real de teste, de 1.057 eventos, 1.054 ficaram com preço.

## Segurança implementada

**Login e sessão**
- Mensagem de login no formato SIWS, presa ao domínio do front. Nonce de uso único com 5 minutos.
- Código de e-mail com 6 dígitos, 10 minutos, 5 tentativas. No Redis ficam só HMACs, nunca o e-mail ou o código.
- Google e GitHub com `state` de uso único e PKCE. O token de sessão nunca passa por URL: o front troca um código de 60 segundos.
- Token de sessão opaco; no banco só o SHA-256. Logout revoga.
- Rate limit por IP e por usuário nas rotas de login, leitura, relatório e agente.

**Dados**
- Leitura de dados do usuário sempre com RLS. Testes cobrem um usuário tentando ver ou alterar carteira, evento, relatório e conversa de outro.
- Os triggers do banco impedem trocar dono, quantidade ou ativo de um evento.
- Conteúdo vindo da blockchain (nome de token, descrição) é tratado como dado: símbolos são limpos, descrições não são guardadas, células de CSV que parecem fórmula são neutralizadas.

**Agente**
- Recebe só os dados do próprio usuário, dentro de um bloco marcado como dado. Não tem ferramenta nenhuma.
- Devolve um objeto estruturado. Links e citações são montados pelo servidor a partir de referências, nunca pelo modelo.
- A pergunta só é descontada da cota depois que o modelo responde.

**Relatório**
- CSV final imutável no R2, em bucket privado, baixado por link assinado de 10 minutos.
- O hash gravado na Solana é o SHA-256 do arquivo. Uma linha com código aleatório impede descobrir os valores por tentativa.
- A verificação pública devolve só mês, hash e transação.

**Infraestrutura**
- Cada container recebe só as variáveis que usa. A chave da carteira de memo e a da Helius existem só no worker; OpenAI, Resend e R2 só na API.
- Containers sem root, disco somente leitura, sem capabilities. Redis em rede interna, com senha.
- Cloudflare na frente com SSL Full (strict), TLS 1.2 no mínimo e Authenticated Origin Pulls. Firewall da droplet aceita 80 e 443 só da Cloudflare.
- Logs do servidor sem tokens, sem chaves e sem endereços completos. Conferido depois do teste em produção.

**Verificações antes de cada entrega:** 143 testes, ruff, mypy strict e bandit, todos sem achados em 03/10.

## O que falta

| Item | Efeito hoje | Quem resolve |
|---|---|---|
| SOL na carteira de memo (devnet) | O relatório finaliza, mas a transação na Solana não é gravada e `attestation` fica `null`. A gravação é tentada de novo a cada consulta do relatório | Erik: abastecer pelo faucet |
| Google e GitHub | Desligados | Erik: criar os apps OAuth |
| Chave da CoinGecko | Funciona sem chave, com limite menor. Ativos pouco negociados podem ficar sem preço até a rodada seguinte | Erik ou Filipe |
| Leiaute oficial da DeCripto | O arquivo gerado é um resumo de apoio, não o leiaute de transmissão da Receita | Precisa do manual do leiaute |
| Revisão das regras fiscais | Os números são estimativa | Contador |
| Conta do GitHub bloqueada por cobrança | Nenhum workflow roda. O deploy é manual | Erik |
| Token do R2 restrito ao bucket | O token atual é mais amplo que o necessário | Erik |
| Classificação com modelo de linguagem | Não implementada. A classificação por variação de saldo cobriu os casos testados | Avaliar depois do beta |
| Publicar o código de 03/10 | Rotas novas ainda não estão no ar | Erik: ligar o ssh-agent e rodar o deploy |
| Telas novas no front | Ver `docs/FRONT_CHANGES.md` | Filipe |

## Rodar local

```bash
uv sync
cp .env.example .env                       # valores reais com o Erik
docker run -d --name orbix-redis -p 127.0.0.1:6379:6379 redis:7.4-alpine
uv run uvicorn orbix.main:app --reload     # API
uv run arq orbix.worker.WorkerSettings     # worker
```

Testes (precisam de Docker para o Postgres local, que é uma cópia do schema de produção):

```bash
uv run python scripts/testdb.py up
uv run pytest -q && uv run ruff check . && uv run mypy src && uv run bandit -q -r src
```

## Deploy

Manual, da máquina do Erik: `.\scripts\deploy\deploy.ps1 -HostIp <ip>`. O script monta a imagem, gera os arquivos de ambiente a partir do `.env` local, envia por SSH e sobe os containers. A senha do Redis e a chave de assinatura que já estão no servidor são preservadas. Migrações são aplicadas antes, à mão: `uv run alembic upgrade head`.

Os manifests de Kubernetes em `infra/k8s/` estão prontos e validados contra o schema, mas não estão em uso. Exigem um nó com 2 GB ou mais.
