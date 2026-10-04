"""Dependências do FastAPI: recursos compartilhados e o usuário da requisição."""

from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from fastapi import Depends, Request
from redis.asyncio import Redis

from orbix.auth import service as auth_service
from orbix.config import Settings, get_settings
from orbix.db import Database
from orbix.errors import unauthorized


class Queue(Protocol):
    async def enqueue(self, job: str, *args: Any, job_id: str | None = None) -> None: ...


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    session_id: UUID


def get_db(request: Request) -> Database:
    return cast(Database, request.app.state.db)


def get_redis(request: Request) -> Redis:
    return cast(Redis, request.app.state.redis)


def get_queue(request: Request) -> Queue:
    return cast(Queue, request.app.state.queue)


def settings_dep() -> Settings:
    return get_settings()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 200:
        return None
    return token.strip()


async def optional_user(request: Request, db: Database = Depends(get_db)) -> CurrentUser | None:
    token = _bearer(request)
    if token is None:
        return None
    async with db.service() as conn:
        found = await auth_service.find_session(conn, token)
    if found is None:
        return None
    user_id, session_id = found
    # usado pelo rate limit por usuário
    request.state.user_id = str(user_id)
    return CurrentUser(id=user_id, session_id=session_id)


async def current_user(user: CurrentUser | None = Depends(optional_user)) -> CurrentUser:
    if user is None:
        raise unauthorized()
    return user


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
