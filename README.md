# orbix-core

Back-end do **Orbix Declare**: lê carteiras Solana e Hyperliquid, converte cada evento para reais
pela PTAX do Banco Central, calcula o relatório mensal, explica os números com um agente de IA e
registra o hash do relatório na Solana.

- Estado atual, rotas, regras de cálculo e pendências: [docs/STATUS.md](docs/STATUS.md)
- O que o front precisa mudar: [docs/FRONT_CHANGES.md](docs/FRONT_CHANGES.md)

## Stack

- Python 3.12, FastAPI, Pydantic v2, asyncpg; migrações com Alembic
- arq + Redis para jobs (leitura de carteiras, cotação, gravação do hash)
- Postgres no Supabase, com RLS. O login é do próprio back-end (carteira, e-mail, Google, GitHub)
- Helius (Solana), API pública da Hyperliquid, CoinGecko, PTAX do Banco Central
- OpenAI para o agente; Resend para e-mail; Cloudflare R2 para arquivos
- Docker Compose na DigitalOcean, atrás da Cloudflare

## Rodar local

```bash
uv sync
cp .env.example .env              # preencher
docker run -d --name orbix-redis -p 127.0.0.1:6379:6379 redis:7.4-alpine
uv run uvicorn orbix.main:app --reload
uv run arq orbix.worker.WorkerSettings
```

## Testes e verificações

Os testes de integração usam um Postgres local igual ao schema de produção (precisa de Docker).

```bash
uv run python scripts/testdb.py up
uv run pytest -q
uv run ruff check .
uv run mypy src
uv run bandit -q -r src
uvx pre-commit install            # gitleaks + ruff antes de cada commit
```

## Migrações

```bash
uv run alembic upgrade head       # usa MIGRATION_DATABASE_URL
```

Mudança de banco só por migração neste repositório. Tabela nova em `public` precisa de RLS e de
`revoke` dos acessos automáticos de `anon` e `authenticated` na mesma migração.

## Funding da Hyperliquid

A Hyperliquid cobra funding de hora em hora. O Orbix não agrupa nada: grava um evento para cada lançamento que a API `userFunding` devolve. Quem consolida é a própria Hyperliquid. Para períodos antigos ela devolve um lançamento por dia, com o campo `nSamples` dizendo quantas cobranças horárias ele soma; para períodos recentes devolve cada hora.

Por isso o mesmo dia pode aparecer como um evento só ou como vários, dependendo de quando a carteira foi lida. A soma do funding de um dia no app tem de bater com a soma dos lançamentos desse dia na API. Funding recebido entra como ganho e funding pago como custo, convertidos pela PTAX do dia.

## CSV do relatório

Colunas, nesta ordem (as novas entram sempre no fim):

`data,tipo,ativo,quantidade,ptax,valor_brl,custo_brl,ganho_brl,preco_manual,taxas_brl,custo_desconhecido,rede,carteira,custo_informado`

- A linha `total` é a soma exata das linhas.
- No relatório final há uma última linha `verificacao,<código>`: um código aleatório que entra no hash do arquivo. Sem ele, alguém poderia descobrir os valores testando combinações contra o hash público. O rascunho não tem essa linha; o que marca o rascunho é o `-rascunho` no nome do arquivo.

## Modelo de segurança

- A API conecta como `orbix_api` (sem `BYPASSRLS`). Para ler dados do usuário, cada requisição
  assume o papel `authenticated` com o id dele, e o RLS do Postgres isola os dados.
- O worker conecta como `orbix_worker`, com grants mínimos e sem `DELETE`.
- O usuário é criado por uma função restrita do banco. A chave `service_role` do Supabase não é usada.
- Token de sessão opaco, guardado só como SHA-256. Códigos de e-mail e nonces de uso único no Redis.
- Só o container do worker recebe a chave da carteira de memo.
- O hash gravado na Solana é o SHA-256 do CSV, que leva um código aleatório; nenhum dado pessoal vai on-chain.
- O agente só lê dados do próprio usuário e não tem ferramentas; links de citação são montados pelo servidor.
- Containers sem root, disco somente leitura e sem capabilities; só o Caddy publica portas, e só para a Cloudflare.
- CI: ruff, mypy strict, pytest, bandit, pip-audit, Trivy e gitleaks.

O Orbix Declare nunca pede chave privada, frase de recuperação ou assinatura de transação.
