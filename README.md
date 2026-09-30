# orbix-core

Back-end do **Orbix Declare**: lê carteiras Solana e Hyperliquid, converte cada evento para reais
pela PTAX do Banco Central, calcula a DeCripto do mês, explica com um agente de IA e registra o
hash do relatório na Solana.

## Stack

- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 async + asyncpg, Alembic
- arq + Redis para jobs (ingestão, preços, atestação)
- Supabase (Postgres com RLS + Auth Sign-In With Solana)
- Cloudflare R2 para arquivos, Cloudflare na frente da API
- Docker na DigitalOcean (Caddy + API + worker + Redis)

## Rodar local

```bash
uv sync
cp .env.example .env              # preencher
docker run -d --name orbix-redis -p 127.0.0.1:6379:6379 redis:7.4-alpine
uv run uvicorn orbix.main:app --reload
uv run arq orbix.worker.WorkerSettings
```

## Qualidade e segurança

```bash
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

## Modelo de segurança

- A API conecta como `orbix_api` (sem `BYPASSRLS`) e cada requisição roda como `authenticated`
  com as claims do JWT do usuário: o RLS do Postgres isola os dados.
- O worker conecta como `orbix_worker`, com grants mínimos e sem `DELETE`.
- JWT do Supabase validado com lista fechada de algoritmos, `aud`, `iss` e `exp`.
- Só o container do worker recebe a chave da carteira de memo.
- O hash gravado na Solana usa salt aleatório; nenhum dado pessoal vai on-chain.
- Containers sem root, sistema de arquivos somente leitura e sem capabilities; só o Caddy
  publica portas, e só para a Cloudflare.
- CI: ruff, mypy strict, pytest, bandit, pip-audit, Trivy e gitleaks.

O Orbix Declare nunca pede chave privada, seed phrase ou assinatura de transação.
