from datetime import date
from typing import Literal, cast
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis

from orbix.db import Database
from orbix.deps import CurrentUser, Queue, current_user, get_db, get_queue, get_redis
from orbix.i18n import sync_error_message, tr
from orbix.queue import ACTIVE_TTL, active_key, sync_user_job_id
from orbix.ratelimit import limiter
from orbix.schemas import Network, StepState, SyncStatusOut, SyncStepOut, SyncWalletOut

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

WALLET_STATE: dict[str, StepState] = {
    "pending": "pending",
    "syncing": "running",
    "synced": "done",
    "empty": "done",
    "error": "error",
}


def _aggregate(states: list[StepState]) -> StepState:
    if "running" in states:
        return "running"
    if "pending" in states:
        return "pending"
    if states and all(s == "error" for s in states):
        return "error"
    return "done"


def _network_step(key: str, state: StepState) -> SyncStepOut:
    labels = {
        "solana": {
            "done": tr("Histórico Solana lido", "Solana history read"),
            "running": tr("Lendo o histórico Solana", "Reading Solana history"),
            "pending": tr("Histórico Solana na fila", "Solana history queued"),
            "error": tr("Falha ao ler o histórico Solana", "Failed to read Solana history"),
        },
        "hyperliquid": {
            "done": tr("Fills e funding da Hyperliquid lidos", "Hyperliquid fills and funding read"),
            "running": tr(
                "Buscando fills e funding na Hyperliquid", "Fetching Hyperliquid fills and funding"
            ),
            "pending": tr("Hyperliquid na fila", "Hyperliquid queued"),
            "error": tr("Falha ao ler a Hyperliquid", "Failed to read Hyperliquid"),
        },
    }
    return SyncStepOut(key=key, label=labels[key][state], state=state)


async def build_status(conn: asyncpg.Connection, redis: Redis, user_id: UUID) -> SyncStatusOut:
    """`conn` precisa estar em `as_user`: o RLS limita as linhas ao próprio usuário."""
    rows = await conn.fetch(
        "select id, chain, address, status, sync_read, sync_total, sync_error, verified "
        "from public.wallets order by is_login desc, created_at"
    )
    since = await conn.fetchval("select min(ts)::date from public.events")
    onboarded = await conn.fetchval(
        "select onboarded_at is not null from public.profiles where id = $1", user_id
    )
    active = bool(await redis.exists(active_key(user_id)))

    wallets = [
        SyncWalletOut(
            wallet_id=str(r["id"]),
            network=cast(Network, r["chain"]),
            address=r["address"],
            read=r["sync_read"],
            total=r["sync_total"],
            state=WALLET_STATE[r["status"]],
            detail=tr("fills e funding", "fills and funding") if r["chain"] == "hyperliquid" else None,
            error=sync_error_message(r["sync_error"]) if r["status"] == "error" else None,
        )
        for r in rows
    ]
    states = [w.state for w in wallets]
    reading = _aggregate(states) if wallets else "pending"

    steps = [
        SyncStepOut(
            key="verify",
            label=tr("Carteira verificada por assinatura", "Wallet verified by signature")
            if any(r["verified"] for r in rows)
            else tr("Conta confirmada", "Account confirmed"),
            state="done",
        )
    ]
    for network in ("solana", "hyperliquid"):
        own = [w.state for w in wallets if w.network == network]
        if own:
            steps.append(_network_step(network, _aggregate(own)))

    finished_reading = bool(wallets) and reading in ("done", "error")
    prices_state: StepState = (
        "running" if finished_reading and active else "done" if finished_reading else "pending"
    )
    steps.append(
        SyncStepOut(
            key="prices",
            label=tr("Cotando cada evento pela PTAX", "Pricing each event at the PTAX rate"),
            state=prices_state,
        )
    )
    steps.append(
        SyncStepOut(
            key="dashboard",
            label=tr("Montando o painel do mês", "Building this month's dashboard"),
            state="done" if onboarded and not active else "pending",
        )
    )

    if not wallets or (all(s == "pending" for s in states) and not active):
        overall: Literal["idle", "running", "done", "error"] = "idle"
    elif active or "running" in states or "pending" in states:
        overall = "running"
    elif all(s == "error" for s in states):
        overall = "error"
    else:
        overall = "done"

    totals = [w.total for w in wallets]
    return SyncStatusOut(
        state=overall,
        since=(since or date.today()).isoformat(),
        read=sum(w.read for w in wallets),
        estimated=sum(t for t in totals if t is not None)
        if totals and all(t is not None for t in totals)
        else None,
        wallets=wallets,
        steps=steps,
    )


@router.post("", response_model=SyncStatusOut)
@limiter.limit("10/minute")
async def start_sync(
    request: Request,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    redis: Redis = Depends(get_redis),
    queue: Queue = Depends(get_queue),
) -> SyncStatusOut:
    """Inicia ou retoma a leitura de todas as carteiras. Chamar de novo não duplica o trabalho."""
    async with db.service() as conn:
        started = await conn.fetch(
            "update public.wallets set status = 'syncing', sync_error = null, sync_started_at = now() "
            "where user_id = $1 and status <> 'syncing' returning id",
            user.id,
        )
        has_wallets = await conn.fetchval(
            "select exists(select 1 from public.wallets where user_id = $1)", user.id
        )
    if has_wallets:
        if started:
            await redis.set(active_key(user.id), 1, ex=ACTIVE_TTL)
        # com o mesmo job_id, a fila ignora o pedido enquanto a leitura anterior ainda roda
        await queue.enqueue("sync_user", str(user.id), job_id=sync_user_job_id(user.id))
    async with db.as_user(user.id) as conn:
        return await build_status(conn, redis, user.id)


@router.get("/status", response_model=SyncStatusOut)
async def sync_status(
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> SyncStatusOut:
    async with db.as_user(user.id) as conn:
        return await build_status(conn, redis, user.id)
