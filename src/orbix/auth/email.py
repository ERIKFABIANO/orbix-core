"""Login por e-mail com código de 6 dígitos, sem senha.

No Redis ficam só HMACs: nem o e-mail nem o código aparecem em claro.
"""

from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from orbix.auth.mailer import Mailer
from orbix.config import Settings
from orbix.errors import AppError
from orbix.i18n import get_locale
from orbix.security import keyed_hash, numeric_code, same

CODE_TTL = 600
RESEND_AFTER = 60
MAX_ATTEMPTS = 5
MAX_PER_EMAIL_HOUR = 5
MAX_PER_IP_HOUR = 15


def normalize(email: str) -> str:
    return email.strip().lower()


async def _bump(redis: Redis, key: str, ttl: int) -> int:
    count = int(await redis.incr(key))
    if count == 1:
        await redis.expire(key, ttl)
    return count


async def start(
    redis: Redis, settings: Settings, mailer: Mailer, email: str, ip: str
) -> tuple[datetime, int]:
    email = normalize(email)
    eid = keyed_hash(settings.signing_key, "email", email)

    cooldown = int(await redis.ttl(f"auth:email:cd:{eid}"))
    if cooldown > 0:
        raise AppError("rate_limited", 429, seconds=cooldown)
    if (
        await _bump(redis, f"auth:email:ip:{keyed_hash(settings.signing_key, 'ip', ip)}", 3600)
        > MAX_PER_IP_HOUR
    ):
        raise AppError("rate_limited", 429, seconds=3600)
    if await _bump(redis, f"auth:email:hr:{eid}", 3600) > MAX_PER_EMAIL_HOUR:
        raise AppError("rate_limited", 429, seconds=3600)

    code = numeric_code()
    key = f"auth:email:code:{eid}"
    await redis.delete(key)
    await redis.hset(
        key,
        # type: ignore[misc]
        mapping={"hash": keyed_hash(settings.signing_key, "code", email, code), "attempts": 0},
    )
    await redis.expire(key, CODE_TTL)
    await redis.set(f"auth:email:cd:{eid}", 1, ex=RESEND_AFTER)

    try:
        await mailer.send_login_code(email, code, get_locale())
    except AppError:
        # sem e-mail enviado, não faz sentido prender a pessoa no intervalo de reenvio
        await redis.delete(key, f"auth:email:cd:{eid}")
        raise
    return datetime.now(UTC) + timedelta(seconds=CODE_TTL), RESEND_AFTER


async def verify(redis: Redis, settings: Settings, email: str, code: str) -> str:
    """Confere o código. Devolve o e-mail normalizado e confirmado."""
    email = normalize(email)
    key = f"auth:email:code:{keyed_hash(settings.signing_key, 'email', email)}"
    stored = await redis.hget(key, "hash")  # type: ignore[misc]
    if stored is None:
        raise AppError("code_expired", 401)

    # conta a tentativa antes de comparar: tentativas em paralelo não furam o limite
    attempts = int(await redis.hincrby(key, "attempts", 1))  # type: ignore[misc]
    if attempts > MAX_ATTEMPTS:
        await redis.delete(key)
        raise AppError("too_many_attempts", 429)
    if not same(stored.decode(), keyed_hash(settings.signing_key, "code", email, code)):
        if attempts >= MAX_ATTEMPTS:
            await redis.delete(key)
            raise AppError("too_many_attempts", 429)
        raise AppError("invalid_code", 401)

    await redis.delete(key)
    return email
