"""Worker (arq): leitura de carteiras, cotação e gravação do hash na Solana.

Conecta no banco como `orbix_worker`. É o único processo que recebe a chave da carteira de memo.
"""

from typing import Any, ClassVar
from uuid import UUID

import httpx
import structlog
from arq import Retry, cron, func
from arq.connections import RedisSettings

from orbix.attest import memo
from orbix.config import get_settings
from orbix.db import Database
from orbix.ingest.helius import HeliusClient, ipv4_client
from orbix.ingest.hyperliquid import HyperliquidClient
from orbix.ingest.sync import Sources, price_and_finish, sync_wallet
from orbix.logging import configure_logging
from orbix.prices import ptax
from orbix.queue import ACTIVE_TTL, active_key

log = structlog.get_logger()


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(json_logs=settings.is_prod)
    dsn = settings.worker_database_url or settings.database_url
    db = Database(dsn.get_secret_value(), ssl=settings.db_ssl, max_size=4)
    await db.connect()
    http = httpx.AsyncClient(timeout=30, headers={"User-Agent": "orbix-declare/0.1"})
    helius_http = ipv4_client()
    helius_key = settings.helius_api_key
    ctx["settings"] = settings
    ctx["db"] = db
    ctx["http"] = http
    ctx["helius_http"] = helius_http
    ctx["sources"] = Sources(
        http=http,
        helius=HeliusClient(helius_key.get_secret_value(), helius_http) if helius_key else None,
        hyperliquid=HyperliquidClient(http),
        coingecko_key=settings.coingecko_api_key.get_secret_value() if settings.coingecko_api_key else None,
        max_transactions=settings.ingest_max_transactions,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["http"].aclose()
    await ctx["helius_http"].aclose()
    await ctx["db"].close()


async def _finish(ctx: dict[str, Any], user_id: UUID) -> None:
    try:
        summary = await price_and_finish(ctx["db"], ctx["sources"], user_id)
        log.info("cotação concluída", **summary)
    finally:
        await ctx["redis"].delete(active_key(user_id))


async def sync_wallet_job(ctx: dict[str, Any], wallet_id: str) -> None:
    db: Database = ctx["db"]
    async with db.service() as conn:
        owner = await conn.fetchval("select user_id from public.wallets where id = $1", UUID(wallet_id))
    if owner is None:
        return
    await ctx["redis"].set(active_key(owner), 1, ex=ACTIVE_TTL)
    await sync_wallet(db, ctx["sources"], UUID(wallet_id))
    await _finish(ctx, owner)


async def sync_user_job(ctx: dict[str, Any], user_id: str) -> None:
    db: Database = ctx["db"]
    user = UUID(user_id)
    await ctx["redis"].set(active_key(user), 1, ex=ACTIVE_TTL)
    async with db.service() as conn:
        wallets = await conn.fetch(
            "select id from public.wallets where user_id = $1 and status in ('pending', 'syncing') "
            "order by is_login desc, created_at",
            user,
        )
    for wallet in wallets:
        # cada carteira renova a marca de atividade: a leitura pode demorar
        await ctx["redis"].set(active_key(user), 1, ex=ACTIVE_TTL)
        await sync_wallet(db, ctx["sources"], wallet["id"])
    await _finish(ctx, user)


# a cada rodada, no máximo isto de usuários: a cota da CoinGecko é dividida entre eles
REPRICE_USERS_PER_RUN = 20


async def reprice_pending(ctx: dict[str, Any]) -> None:
    """Tenta de novo o preço de eventos que ficaram sem valor (ex.: CoinGecko pediu para esperar)."""
    db: Database = ctx["db"]
    # evento que ficou com a PTAX da véspera porque a do dia ainda não tinha saído
    refreshed = 0
    try:
        async with db.service() as conn:
            refreshed = await ptax.refresh_stale(conn, ctx["sources"].http)
    except Exception:
        # a correção da PTAX é independente: se falhar, os eventos sem preço ainda são cotados
        log.exception("recotação da ptax falhou")
    if refreshed:
        log.info("ptax do dia aplicada", events=refreshed)
    async with db.service() as conn:
        users = await conn.fetch(
            """
            select distinct user_id from public.events
             where brl_value is null and pricing_policy is distinct from 'manual'
               and kind in ('swap_in', 'swap_out', 'perp_fill', 'funding')
             limit $1
            """,
            REPRICE_USERS_PER_RUN,
        )
    for row in users:
        user_id = row["user_id"]
        # usuário com leitura em andamento: a própria leitura cota no fim
        if await ctx["redis"].exists(active_key(user_id)):
            continue
        summary = await price_and_finish(db, ctx["sources"], user_id)
        log.info("recotação", **summary)


async def attest_report_job(ctx: dict[str, Any], report_id: str) -> None:
    db: Database = ctx["db"]
    settings = ctx["settings"]
    redis = ctx["redis"]
    report = UUID(report_id)
    async with db.service() as conn:
        row = await conn.fetchrow(
            "select sha256, solana_sig from public.reports where id = $1 and status = 'final'", report
        )
    if row is None or row["solana_sig"]:
        return

    async def fail(reason: str) -> None:
        async with db.service() as conn:
            await conn.execute(
                "update public.reports set attest_error = $2 where id = $1", report, reason[:200]
            )

    secret = settings.solana_memo_secret_key
    if secret is None:
        await fail("not_configured")
        return
    url = settings.solana_memo_rpc_url.get_secret_value()
    pending_key = f"attest:pending:{report}"
    try:
        # se uma tentativa anterior já enviou a transação, não enviar de novo: só conferir
        pending = await redis.get(pending_key)
        signature = pending.decode() if pending else None
        slot = await memo.confirmed_slot(ctx["http"], url, signature) if signature else None
        if slot is None:
            if signature is None:
                signature = await memo.submit_memo(
                    ctx["http"],
                    url,
                    memo.load_keypair(secret.get_secret_value()),
                    memo.memo_text(row["sha256"]),
                )
                # o blockhash vale cerca de 2 minutos; depois disso a transação não entra mais
                await redis.set(pending_key, signature, ex=150)
            slot = await memo.wait_confirmation(ctx["http"], url, signature)
    except memo.AttestError as exc:
        log.warning("atestação falhou", reason=str(exc))
        await fail(str(exc))
        raise Retry(defer=ctx.get("job_try", 1) * 30) from None

    async with db.service() as conn:
        await conn.execute(
            "update public.reports set solana_sig = $2, slot = $3, attested_at = now(), "
            "attest_error = null where id = $1",
            report,
            signature,
            slot,
        )
        # versão imutável do relatório, já com a transação que a comprova
        await conn.execute(
            """
            insert into public.report_versions
              (report_id, user_id, snapshot, rules_version, r2_object_key, sha256, solana_sig)
            select id, user_id, data, rules_version, r2_object_key, sha256, solana_sig
              from public.reports where id = $1
            """,
            report,
        )
    await redis.delete(pending_key)


class WorkerSettings:
    functions: ClassVar[list[Any]] = [
        # keep_result=0 libera o job_id assim que termina, para poder enfileirar de novo
        func(sync_wallet_job, name="sync_wallet", keep_result=0, timeout=900, max_tries=1),
        func(sync_user_job, name="sync_user", keep_result=0, timeout=1800, max_tries=1),
        func(attest_report_job, name="attest_report", keep_result=0, timeout=180, max_tries=5),
    ]
    cron_jobs: ClassVar[list[Any]] = [
        cron(reprice_pending, name="reprice_pending", minute={13, 43}, timeout=600, keep_result=0),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url.get_secret_value())
    max_jobs = 4  # droplet de 1 GB
