from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from orbix.config import get_settings
from orbix.errors import register_error_handlers
from orbix.logging import configure_logging
from orbix.middleware import RequestContextMiddleware
from orbix.ratelimit import limiter
from orbix.routers import health, me


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(json_logs=settings.is_prod)

    app = FastAPI(
        title="Orbix Declare API",
        version="0.1.0",
        # sem documentação pública em produção
        docs_url=None if settings.is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_prod else "/openapi.json",
    )
    app.state.limiter = limiter

    # ordem: o último adicionado roda primeiro
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,  # nunca "*"
        allow_credentials=False,  # Bearer no header, sem cookie: sem CSRF
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(me.router)
    return app


app = create_app()
