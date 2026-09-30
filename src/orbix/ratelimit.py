from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from orbix.config import get_settings


def user_or_ip(request: Request) -> str:
    """Chave do rate limit: id do usuário autenticado ou, sem sessão, o IP real.

    O IP real chega via Cloudflare → Caddy (CF-Connecting-IP) → X-Forwarded-For,
    e o Uvicorn roda com --proxy-headers.
    """
    user_id = getattr(request.state, "user_id", None)
    return f"user:{user_id}" if user_id else f"ip:{get_remote_address(request)}"


def build_limiter() -> Limiter:
    settings = get_settings()
    storage = "memory://" if settings.env == "test" else settings.redis_url.get_secret_value()
    return Limiter(key_func=user_or_ip, storage_uri=storage, default_limits=["120/minute"])


limiter = build_limiter()
