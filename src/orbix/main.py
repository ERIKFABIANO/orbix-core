from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from arq.connections import RedisSettings, create_pool
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from orbix.agent.llm import OpenAIAgent
from orbix.auth.mailer import ResendMailer
from orbix.config import get_settings
from orbix.db import Database
from orbix.errors import register_error_handlers
from orbix.logging import configure_logging
from orbix.middleware import RequestContextMiddleware
from orbix.queue import ArqQueue
from orbix.ratelimit import limiter
from orbix.routers import agent, auth, dashboard, health, ingest, me, reports, verify, wallets
from orbix.storage import R2Storage


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    db = Database(settings.database_url.get_secret_value(), ssl=settings.db_ssl)
    await db.connect()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url.get_secret_value()))
    http = httpx.AsyncClient(timeout=15, follow_redirects=False, headers={"User-Agent": "orbix-declare/0.1"})

    app.state.db = db
    app.state.redis = redis
    app.state.queue = ArqQueue(redis)
    app.state.http = http
    app.state.mailer = ResendMailer(settings, http)
    app.state.storage = R2Storage(settings) if settings.storage_enabled else None
    llm = (
        OpenAIAgent(settings.openai_api_key.get_secret_value(), settings.llm_model_agent)
        if settings.openai_api_key
        else None
    )
    app.state.llm = llm
    try:
        yield
    finally:
        if llm is not None:
            await llm.close()
        await http.aclose()
        await redis.aclose()
        await db.close()


def create_app(state: dict[str, Any] | None = None) -> FastAPI:
    """`state` permite aos testes injetar banco, Redis, fila e e-mail falsos, sem o lifespan."""
    settings = get_settings()
    configure_logging(json_logs=settings.is_prod)

    app = FastAPI(
        title="Orbix Declare API",
        version="0.2.0",
        lifespan=None if state is not None else lifespan,
        # sem documentação pública em produção
        docs_url=None if settings.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_prod else "/openapi.json",
    )
    app.state.limiter = limiter
    for name, value in (state or {}).items():
        setattr(app.state, name, value)

    # ordem: o último adicionado roda primeiro
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,  # nunca "*"
        allow_credentials=False,  # Bearer no header, sem cookie: sem CSRF
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Accept-Language"],
        max_age=600,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    register_error_handlers(app)
    for module in (health, auth, me, wallets, ingest, dashboard, reports, verify, agent):
        app.include_router(module.router)
    return app


app = create_app()
