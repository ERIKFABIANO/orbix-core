import json
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from orbix.auth import User, current_user
from orbix.config import get_settings


@lru_cache
def engine() -> AsyncEngine:
    settings = get_settings()
    connect_args: dict[str, Any] = {
        # pooler do Supabase em modo transação não aceita prepared statements em cache
        "statement_cache_size": 0,
    }
    if settings.db_ssl:
        connect_args["ssl"] = "require"
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args=connect_args,
    )


@lru_cache
def session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine(), expire_on_commit=False)


async def user_session(user: User = Depends(current_user)) -> AsyncIterator[AsyncSession]:
    """Sessão com RLS: a transação roda como `authenticated` com as claims do usuário.

    A conexão usa o papel `orbix_api` (sem BYPASSRLS). `SET LOCAL` vale só dentro
    desta transação, então nada vaza para a próxima requisição que pegar a conexão.
    """
    async with session_factory()() as session, session.begin():
        await session.execute(
            text("select set_config('request.jwt.claims', :claims, true)"),
            {"claims": json.dumps(user.claims)},
        )
        await session.execute(text("set local role authenticated"))
        yield session
