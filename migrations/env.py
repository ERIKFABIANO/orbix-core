import asyncio
import os
from pathlib import Path

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine


def _migration_url() -> str:
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        # desenvolvimento local: lê do .env da raiz sem carregar o resto das configurações
        env_file = Path(__file__).resolve().parents[1] / ".env"
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("MIGRATION_DATABASE_URL="):
                url = line.split("=", 1)[1].strip()
    if not url:
        raise SystemExit("defina MIGRATION_DATABASE_URL")
    return url


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=None, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    engine = create_async_engine(
        _migration_url(), connect_args={"ssl": "require", "statement_cache_size": 0}
    )
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    raise SystemExit("modo offline desativado: gere SQL revisando o arquivo da migração")
asyncio.run(run_online())
