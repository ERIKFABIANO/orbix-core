from typing import cast
from urllib.parse import quote
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis

from orbix.auth import email as email_flow
from orbix.auth import oauth, wallet
from orbix.auth import service as auth_service
from orbix.auth.mailer import Mailer
from orbix.config import Settings
from orbix.db import Database
from orbix.deps import (
    CurrentUser,
    client_ip,
    current_user,
    get_db,
    get_redis,
    optional_user,
    settings_dep,
)
from orbix.errors import AppError
from orbix.i18n import get_locale
from orbix.ratelimit import limiter
from orbix.schemas import (
    EmailStartIn,
    EmailStartOut,
    EmailVerifyIn,
    ExchangeIn,
    LoginMethod,
    NonceOut,
    OAuthStartOut,
    SessionOut,
    VerifyIn,
)

log = structlog.get_logger()
router = APIRouter(prefix="/api/auth", tags=["auth"])


async def _open_session(
    request: Request, db: Database, settings: Settings, user_id: UUID, method: LoginMethod
) -> SessionOut:
    async with db.service() as conn:
        token, expires_at = await auth_service.create_session(
            conn, settings, user_id, method, request.headers.get("user-agent")
        )
        user = await auth_service.load_user(conn, settings, user_id)
    if user is None:
        raise AppError("unauthorized", 401)
    return SessionOut(token=token, expires_at=expires_at, user=user)


@router.get("/providers")
async def providers(settings: Settings = Depends(settings_dep)) -> dict[str, bool]:
    return settings.providers


# ── carteira ──────────────────────────────────────────────────────────────


@router.get("/nonce", response_model=NonceOut)
@limiter.limit("30/minute")
async def nonce(
    request: Request,
    address: str = Query(max_length=44),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(settings_dep),
) -> NonceOut:
    message, value, expires_at = await wallet.start(redis, settings, address, request.headers.get("origin"))
    return NonceOut(message=message, nonce=value, expires_at=expires_at)


@router.post("/verify", response_model=SessionOut)
@limiter.limit("20/minute")
async def verify(
    request: Request,
    body: VerifyIn,
    db: Database = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(settings_dep),
    linking: CurrentUser | None = Depends(optional_user),
) -> SessionOut:
    address = await wallet.verify(redis, body.address, body.message, body.signature)
    async with db.service() as conn:
        user_id = await auth_service.login_or_register(
            conn,
            provider="wallet",
            subject=address,
            locale=get_locale(),
            link_to=linking.id if linking else None,
        )
        await auth_service.ensure_login_wallet(conn, user_id, address)
    return await _open_session(request, db, settings, user_id, "wallet")


# ── e-mail ────────────────────────────────────────────────────────────────


@router.post("/email/start", response_model=EmailStartOut)
@limiter.limit("10/minute")
async def email_start(
    request: Request,
    body: EmailStartIn,
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(settings_dep),
) -> EmailStartOut:
    if not settings.providers["email"]:
        raise AppError("provider_unavailable", 503)
    mailer = cast(Mailer, request.app.state.mailer)
    expires_at, resend_after = await email_flow.start(redis, settings, mailer, body.email, client_ip(request))
    return EmailStartOut(expires_at=expires_at, resend_after=resend_after)


@router.post("/email/verify", response_model=SessionOut)
@limiter.limit("20/minute")
async def email_verify(
    request: Request,
    body: EmailVerifyIn,
    db: Database = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(settings_dep),
    linking: CurrentUser | None = Depends(optional_user),
) -> SessionOut:
    address = await email_flow.verify(redis, settings, body.email, body.code)
    async with db.service() as conn:
        user_id = await auth_service.login_or_register(
            conn,
            provider="email",
            subject=address,
            email=address,
            locale=get_locale(),
            link_to=linking.id if linking else None,
        )
    return await _open_session(request, db, settings, user_id, "email")


# ── Google e GitHub ───────────────────────────────────────────────────────


@router.get("/oauth/{provider}/start", response_model=OAuthStartOut)
@limiter.limit("20/minute")
async def oauth_start(
    request: Request,
    provider: str,
    next: str | None = Query(default=None, max_length=200),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(settings_dep),
    linking: CurrentUser | None = Depends(optional_user),
) -> OAuthStartOut:
    url = await oauth.start(redis, settings, provider, next, linking.id if linking else None, get_locale())
    return OAuthStartOut(url=url)


def _back_to_front(settings: Settings, fragment: str) -> RedirectResponse:
    response = RedirectResponse(f"{settings.app_url}/auth/callback#{fragment}", status_code=302)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/oauth/{provider}/callback", include_in_schema=False)
@limiter.limit("30/minute")
async def oauth_callback(
    request: Request,
    provider: str,
    code: str | None = Query(default=None, max_length=2000),
    state: str | None = Query(default=None, max_length=200),
    error: str | None = Query(default=None, max_length=200),
    db: Database = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(settings_dep),
) -> RedirectResponse:
    # qualquer falha volta para o front com um código de erro; nunca mostra detalhe do provedor
    try:
        pending = await oauth.take_state(redis, provider, state)
        if error or not code:
            raise AppError("oauth_failed", 400)
        http = cast(httpx.AsyncClient, request.app.state.http)
        profile = await oauth.fetch_profile(http, settings, provider, code, pending["verifier"])
        link_to = UUID(pending["link_to"]) if pending.get("link_to") else None
        async with db.service() as conn:
            user_id = await auth_service.login_or_register(
                conn,
                provider=cast(LoginMethod, provider),
                subject=profile.subject,
                email=profile.email,
                display_name=profile.name,
                locale=pending.get("locale"),
                link_to=link_to,
            )
        exchange = await oauth.issue_exchange_code(
            redis, oauth.PendingLogin(cast(oauth.Provider, provider), user_id, pending.get("next") or "")
        )
    except AppError as exc:
        return _back_to_front(settings, f"error={quote(exc.code)}")
    fragment = f"code={quote(exchange)}"
    if pending.get("next"):
        fragment += f"&next={quote(pending['next'], safe='/')}"
    return _back_to_front(settings, fragment)


@router.post("/exchange", response_model=SessionOut)
@limiter.limit("20/minute")
async def exchange(
    request: Request,
    body: ExchangeIn,
    db: Database = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(settings_dep),
) -> SessionOut:
    pending = await oauth.take_exchange_code(redis, body.code)
    return await _open_session(request, db, settings, pending.user_id, pending.provider)


# ── sessão ────────────────────────────────────────────────────────────────


@router.post("/logout", status_code=204)
async def logout(user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)) -> Response:
    async with db.service() as conn:
        await auth_service.revoke_session(conn, user.session_id)
    return Response(status_code=204)
