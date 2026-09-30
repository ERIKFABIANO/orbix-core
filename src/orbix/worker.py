from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

from arq.connections import RedisSettings

from orbix.config import get_settings


async def ping(ctx: dict[str, Any]) -> str:
    """Job de fumaça para validar fila e worker. Ingestão entra aqui a partir de 01/10."""
    return "pong"


class WorkerSettings:
    functions: ClassVar[list[Callable[..., Awaitable[Any]]]] = [ping]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url.get_secret_value())
    max_jobs = 4  # droplet de 1 GB
    job_timeout = 300
    max_tries = 3
