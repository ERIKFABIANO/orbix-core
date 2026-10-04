"""Login com Google e GitHub (OAuth 2.0 com state de uso único e PKCE).

O navegador volta para o front só com um código de troca de 60 segundos, no fragmento da URL.
O token de sessão nunca passa por URL.
"""

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode
from uuid import UUID

import httpx
import structlog
from redis.asyncio import Redis

from orbix.config import Settings
from orbix.errors import AppError
from orbix.security import new_token, sha256_hex

log = structlog.get_logger()

Provider = Literal["google", "github"]
STATE_TTL = 600
EXCHANGE_TTL = 60


@dataclass(frozen=True)
class ProviderConfig:
    authorize_url: str
    token_url: str
    scope: str


PROVIDERS: dict[str, ProviderConfig] = {
    "google": ProviderConfig(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",  # noqa: S106  # nosec B106
        scope="openid email profile",
    ),
    "github": ProviderConfig(
        authorize_url="https://github.com/login/oauth/authorize",
        token_url="https://github.com/login/oauth/access_token",  # noqa: S106  # nosec B106
        scope="read:user user:email",
    ),
}


@dataclass(frozen=True)
class Profile:
    subject: str
    email: str
    name: str | None


@dataclass(frozen=True)
class PendingLogin:
    provider: Provider
    user_id: UUID
    next: str


def safe_next(value: str | None) -> str:
    """Só caminho interno: evita redirecionamento aberto para outro site."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return ""
    return value[:200] if all(c.isprintable() for c in value) else ""


def _credentials(settings: Settings, provider: str) -> tuple[str, str]:
    if provider == "google" and settings.google_client_id and settings.google_client_secret:
        return settings.google_client_id, settings.google_client_secret.get_secret_value()
    if provider == "github" and settings.github_client_id and settings.github_client_secret:
        return settings.github_client_id, settings.github_client_secret.get_secret_value()
    raise AppError("provider_unavailable", 503)


def redirect_uri(settings: Settings, provider: str) -> str:
    return f"{settings.api_url}/api/auth/oauth/{provider}/callback"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def start(
    redis: Redis,
    settings: Settings,
    provider: str,
    next_path: str | None,
    link_to: UUID | None,
    locale: str,
) -> str:
    if provider not in PROVIDERS:
        raise AppError("not_found", 404)
    client_id, _ = _credentials(settings, provider)
    state = new_token()
    verifier = new_token(48)
    await redis.set(
        f"auth:oauth:state:{sha256_hex(state)}",
        json.dumps(
            {
                "provider": provider,
                "verifier": verifier,
                "next": safe_next(next_path),
                "link_to": str(link_to) if link_to else None,
                "locale": locale,
            }
        ),
        ex=STATE_TTL,
    )
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(settings, provider),
        "response_type": "code",
        "scope": PROVIDERS[provider].scope,
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
    }
    if provider == "google":
        params["prompt"] = "select_account"
    return f"{PROVIDERS[provider].authorize_url}?{urlencode(params)}"


async def take_state(redis: Redis, provider: str, state: str | None) -> dict[str, Any]:
    if not state or len(state) > 200:
        raise AppError("oauth_state", 400)
    raw = await redis.getdel(f"auth:oauth:state:{sha256_hex(state)}")
    if raw is None:
        raise AppError("oauth_state", 400)
    data: dict[str, Any] = json.loads(raw)
    if data.get("provider") != provider:
        raise AppError("oauth_state", 400)
    return data


async def fetch_profile(
    client: httpx.AsyncClient, settings: Settings, provider: str, code: str, verifier: str
) -> Profile:
    client_id, client_secret = _credentials(settings, provider)
    try:
        token_response = await client.post(
            PROVIDERS[provider].token_url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri(settings, provider),
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
            timeout=10,
        )
        access_token = (
            token_response.json().get("access_token") if token_response.status_code == 200 else None
        )
        if not isinstance(access_token, str):
            raise AppError("oauth_failed", 400)
        auth = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

        if provider == "google":
            info = (
                await client.get("https://openidconnect.googleapis.com/v1/userinfo", headers=auth, timeout=10)
            ).json()
            if not info.get("sub"):
                raise AppError("oauth_failed", 400)
            if not info.get("email") or info.get("email_verified") is not True:
                raise AppError("email_not_verified", 400)
            return Profile(subject=str(info["sub"]), email=str(info["email"]).lower(), name=info.get("name"))

        user = (await client.get("https://api.github.com/user", headers=auth, timeout=10)).json()
        emails = (await client.get("https://api.github.com/user/emails", headers=auth, timeout=10)).json()
        if not user.get("id"):
            raise AppError("oauth_failed", 400)
        primary = (
            next(
                (
                    e["email"]
                    for e in emails
                    if isinstance(e, dict) and e.get("primary") and e.get("verified")
                ),
                None,
            )
            if isinstance(emails, list)
            else None
        )
        if not primary:
            raise AppError("email_not_verified", 400)
        return Profile(
            subject=str(user["id"]), email=str(primary).lower(), name=user.get("name") or user.get("login")
        )
    except (httpx.HTTPError, ValueError):
        log.warning("falha ao falar com o provedor de login", provider=provider)
        raise AppError("oauth_failed", 400) from None


async def issue_exchange_code(redis: Redis, pending: PendingLogin) -> str:
    code = new_token()
    await redis.set(
        f"auth:oauth:xc:{sha256_hex(code)}",
        json.dumps({"provider": pending.provider, "user_id": str(pending.user_id), "next": pending.next}),
        ex=EXCHANGE_TTL,
    )
    return code


async def take_exchange_code(redis: Redis, code: str) -> PendingLogin:
    raw = await redis.getdel(f"auth:oauth:xc:{sha256_hex(code)}")
    if raw is None:
        raise AppError("code_expired", 401)
    data = json.loads(raw)
    return PendingLogin(provider=data["provider"], user_id=UUID(data["user_id"]), next=data["next"])
