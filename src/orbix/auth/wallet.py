"""Login com carteira Solana (Sign-In With Solana).

O servidor monta a mensagem, guarda o nonce por 5 minutos e só aceita cada nonce uma vez.
"""

import json
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from redis.asyncio import Redis
from solders.pubkey import Pubkey
from solders.signature import Signature

from orbix.config import Settings
from orbix.errors import AppError
from orbix.i18n import tr
from orbix.schemas import is_solana_address
from orbix.security import same

NONCE_TTL = 300


def _key(nonce: str) -> str:
    return f"auth:nonce:{nonce}"


def front_origin(settings: Settings, origin: str | None) -> str:
    """Página que pediu o login. A carteira recusa a assinatura se o domínio da mensagem
    for diferente da página aberta; por isso vale a origem da requisição, mas só se ela
    estiver entre as liberadas no CORS. Caso contrário, o endereço oficial do front."""
    if origin and origin in settings.cors_origins:
        return origin.rstrip("/")
    return settings.app_url


def build_message(
    settings: Settings,
    address: str,
    nonce: str,
    issued: datetime,
    expires: datetime,
    origin: str | None = None,
) -> str:
    # Formato SIWS: as carteiras reconhecem e conferem o domínio contra o site aberto.
    page = front_origin(settings, origin)
    statement = tr(
        "Entrar no Orbix Declare. Esta assinatura não envia transações nem move fundos.",
        "Sign in to Orbix Declare. This signature sends no transactions and moves no funds.",
    )
    return "\n".join(
        [
            f"{urlsplit(page).netloc} wants you to sign in with your Solana account:",
            address,
            "",
            statement,
            "",
            f"URI: {page}",
            "Version: 1",
            "Chain ID: mainnet",
            f"Nonce: {nonce}",
            f"Issued At: {issued.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            f"Expiration Time: {expires.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        ]
    )


async def start(
    redis: Redis, settings: Settings, address: str, origin: str | None = None
) -> tuple[str, str, datetime]:
    if not is_solana_address(address):
        raise AppError("invalid_address", 422)
    nonce = secrets.token_hex(12)
    issued = datetime.now(UTC)
    expires = issued + timedelta(seconds=NONCE_TTL)
    message = build_message(settings, address, nonce, issued, expires, origin)
    await redis.set(_key(nonce), json.dumps({"address": address, "message": message}), ex=NONCE_TTL)
    return message, nonce, expires


def _nonce_of(message: str) -> str | None:
    for line in message.splitlines():
        if line.startswith("Nonce: "):
            value = line.removeprefix("Nonce: ").strip()
            return value if value.isalnum() and len(value) <= 64 else None
    return None


async def verify(redis: Redis, address: str, message: str, signature: str) -> str:
    """Confere nonce e assinatura. Devolve o endereço confirmado."""
    nonce = _nonce_of(message)
    if nonce is None:
        raise AppError("nonce_expired", 401)
    # GETDEL: o nonce some na primeira leitura, então não pode ser reutilizado
    stored_raw = await redis.getdel(_key(nonce))
    if stored_raw is None:
        raise AppError("nonce_expired", 401)
    stored = json.loads(stored_raw)
    # a mensagem assinada tem de ser exatamente a que o servidor emitiu para este endereço
    if not same(stored["address"], address) or not same(stored["message"], message):
        raise AppError("invalid_signature", 401)
    try:
        ok = Signature.from_string(signature).verify(Pubkey.from_string(address), message.encode())
    except Exception:
        ok = False
    if not ok:
        raise AppError("invalid_signature", 401)
    return address
