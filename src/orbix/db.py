"""Acesso ao Postgres com asyncpg.

Dois modos de uso na API:

- `as_user(user_id)`: a transação roda como `authenticated`, com o id do usuário nas claims.
  O RLS do banco filtra tudo. É o caminho padrão para ler e alterar dados do usuário.
- `service()`: a transação roda como o próprio papel de conexão (`orbix_api` ou
  `orbix_worker`). Só para identidade e operações de sistema, sempre com `user_id`
  explícito no SQL.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import UUID

import asyncpg


async def _init_connection(conn: asyncpg.Connection) -> None:
    for kind in ("json", "jsonb"):
        await conn.set_type_codec(kind, encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


class Database:
    def __init__(self, dsn: str, *, ssl: bool, max_size: int = 8) -> None:
        self._dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
        self._ssl = ssl
        self._max_size = max_size
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self._pool = await asyncpg.create_pool(
            self._dsn,
            ssl="require" if self._ssl else None,
            min_size=0,
            max_size=self._max_size,
            # pooler do Supabase em modo transação não aceita prepared statements em cache
            statement_cache_size=0,
            init=_init_connection,
            command_timeout=30,
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("banco não conectado")
        return self._pool

    @asynccontextmanager
    async def service(self) -> AsyncIterator[asyncpg.Connection]:
        async with self.pool.acquire() as conn, conn.transaction():
            yield cast(asyncpg.Connection, conn)

    @asynccontextmanager
    async def as_user(self, user_id: UUID) -> AsyncIterator[asyncpg.Connection]:
        claims = json.dumps({"sub": str(user_id), "role": "authenticated"})
        async with self.pool.acquire() as conn, conn.transaction():
            # SET LOCAL vale só nesta transação: nada vaza para quem pegar a conexão depois
            await conn.execute("select set_config('request.jwt.claims', $1, true)", claims)
            await conn.execute("set local role authenticated")
            yield cast(asyncpg.Connection, conn)


def row_to_dict(row: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
