from typing import cast
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Request, Response
from redis.asyncio import Redis

from orbix.config import Settings
from orbix.db import Database
from orbix.deps import (
    CurrentUser,
    Queue,
    current_user,
    get_db,
    get_queue,
    get_redis,
    settings_dep,
)
from orbix.errors import AppError, not_found
from orbix.i18n import sync_error_message, tr
from orbix.queue import ACTIVE_TTL, active_key, sync_wallet_job_id
from orbix.ratelimit import limiter
from orbix.schemas import (
    Network,
    WalletIn,
    WalletOut,
    WalletStatus,
    is_hyperliquid_address,
    is_solana_address,
)

router = APIRouter(prefix="/api/wallets", tags=["wallets"])

COLUMNS = "id, chain, address, label, is_login, verified_at, last_synced_at, status, sync_error"


def to_wallet(row: asyncpg.Record) -> WalletOut:
    default_label = tr("Carteira Solana", "Solana wallet") if row["chain"] == "solana" else "Hyperliquid"
    return WalletOut(
        id=str(row["id"]),
        network=cast(Network, row["chain"]),
        address=row["address"],
        label=row["label"] or default_label,
        is_login=row["is_login"],
        verified_at=row["verified_at"],
        last_sync_at=row["last_synced_at"],
        status=cast(WalletStatus, row["status"]),
        error=sync_error_message(row["sync_error"]) if row["status"] == "error" else None,
    )


def _wallet_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise not_found() from None


@router.get("", response_model=list[WalletOut])
async def list_wallets(
    user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)
) -> list[WalletOut]:
    async with db.as_user(user.id) as conn:
        rows = await conn.fetch(f"select {COLUMNS} from public.wallets order by is_login desc, created_at")  # noqa: S608  # nosec B608
    return [to_wallet(row) for row in rows]


@router.post("", response_model=WalletOut)
@limiter.limit("20/minute")
async def add_wallet(
    request: Request,
    body: WalletIn,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    queue: Queue = Depends(get_queue),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(settings_dep),
) -> WalletOut:
    valid = (
        is_solana_address(body.address) if body.network == "solana" else is_hyperliquid_address(body.address)
    )
    if not valid:
        raise AppError("invalid_address", 422)
    # Hyperliquid não diferencia maiúsculas: guardar em minúsculas evita duplicata
    address = body.address.lower() if body.network == "hyperliquid" else body.address

    async with db.service() as conn:
        profile = await conn.fetchrow(
            "select plan, (select count(*) from public.wallets where user_id = $1) as total "
            "from public.profiles where id = $1 for update",
            user.id,
        )
        if profile is None:
            raise AppError("unauthorized", 401)
        if profile["plan"] == "free" and profile["total"] >= settings.free_wallet_limit:
            raise AppError("plan_limit", 403, limit=settings.free_wallet_limit)
        try:
            async with conn.transaction():
                row = await conn.fetchrow(
                    f"insert into public.wallets (user_id, chain, address, label, status, sync_started_at) "  # noqa: S608  # nosec B608
                    f"values ($1, $2, $3, $4, 'syncing', now()) returning {COLUMNS}",
                    user.id,
                    body.network,
                    address,
                    body.label or None,
                )
        except asyncpg.UniqueViolationError:
            raise AppError("duplicate_wallet", 409) from None
    if row is None:
        raise AppError("internal", 500)
    # marca a leitura como ativa já: a tela de progresso consulta antes de o worker começar
    await redis.set(active_key(user.id), 1, ex=ACTIVE_TTL)
    await queue.enqueue("sync_wallet", str(row["id"]), job_id=sync_wallet_job_id(row["id"]))
    return to_wallet(row)


@router.delete("/{wallet_id}", status_code=204)
async def remove_wallet(
    wallet_id: str, user: CurrentUser = Depends(current_user), db: Database = Depends(get_db)
) -> Response:
    # o usuário não tem DELETE em wallets: a remoção passa pela API, com o dono no filtro
    async with db.service() as conn:
        row = await conn.fetchrow(
            "select is_login, status from public.wallets where id = $1 and user_id = $2 for update",
            _wallet_id(wallet_id),
            user.id,
        )
        if row is None:
            raise not_found()
        if row["is_login"]:
            raise AppError("login_wallet", 409)
        if row["status"] == "syncing":
            raise AppError("sync_in_progress", 409)
        await conn.execute(
            "delete from public.wallets where id = $1 and user_id = $2", _wallet_id(wallet_id), user.id
        )
    return Response(status_code=204)


@router.post("/{wallet_id}/sync", response_model=WalletOut)
@limiter.limit("10/minute")
async def sync_wallet(
    request: Request,
    wallet_id: str,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    queue: Queue = Depends(get_queue),
    redis: Redis = Depends(get_redis),
) -> WalletOut:
    async with db.service() as conn:
        row = await conn.fetchrow(
            f"update public.wallets set status = 'syncing', sync_error = null, sync_started_at = now() "  # noqa: S608  # nosec B608
            f"where id = $1 and user_id = $2 returning {COLUMNS}",
            _wallet_id(wallet_id),
            user.id,
        )
    if row is None:
        raise not_found()
    # marca a leitura como ativa já: a tela de progresso consulta antes de o worker começar
    await redis.set(active_key(user.id), 1, ex=ACTIVE_TTL)
    await queue.enqueue("sync_wallet", str(row["id"]), job_id=sync_wallet_job_id(row["id"]))
    return to_wallet(row)
