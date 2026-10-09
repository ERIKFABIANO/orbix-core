"""Leitura do histórico de uma carteira e gravação dos eventos. Roda no worker (papel orbix_worker)."""

import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import structlog

from orbix.db import Database
from orbix.ingest import hyperliquid, solana
from orbix.ingest.helius import PAGE_LIMIT, HeliusClient
from orbix.ingest.hyperliquid import HyperliquidClient
from orbix.ingest.models import KNOWN_SYMBOLS, EventDraft, IngestError, is_stable
from orbix.prices.coingecko import CoinGeckoClient
from orbix.prices.service import price_user_events

log = structlog.get_logger()

HL_FILLS_PAGE = 2000
HL_FUNDING_PAGE = 500
# o extrato não documenta o tamanho da página: com 500, uma página cheia sempre pede a seguinte
HL_LEDGER_PAGE = 500


@dataclass
class Sources:
    http: httpx.AsyncClient
    helius: HeliusClient | None
    hyperliquid: HyperliquidClient
    coingecko_key: str | None
    max_transactions: int


INSERT_EVENT = """
insert into public.events
  (wallet_id, user_id, chain, tx_hash, event_index, ts, kind, asset, qty, raw, usd_price,
   pricing_policy)
values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
on conflict (wallet_id, tx_hash, event_index) do update set raw = excluded.raw
  where public.events.raw is distinct from excluded.raw
"""


def _initial_symbol(chain: str, asset: str) -> str | None:
    if chain == "solana":
        return KNOWN_SYMBOLS.get(asset)
    # par spot sem nome da Hyperliquid ("@107"): fica sem símbolo até o spotMeta resolver
    # (relatório de testes de 06/10, B2). Os demais (USDC, HYPE-PERP, PURR/USDC...) já são legíveis.
    if asset.startswith("@"):
        return None
    return asset[:16]


async def _store(
    conn: asyncpg.Connection, wallet: asyncpg.Record, drafts: list[EventDraft], read: int
) -> None:
    chain = wallet["chain"]
    if drafts:
        assets = sorted({d.asset for d in drafts})
        await conn.executemany(
            "insert into public.assets (chain, asset, symbol, is_stable) values ($1, $2, $3, $4) "
            "on conflict (chain, asset) do nothing",
            [(chain, a, _initial_symbol(chain, a), is_stable(chain, a)) for a in assets],
        )
        await conn.executemany(
            INSERT_EVENT,
            [
                (
                    wallet["id"],
                    wallet["user_id"],
                    chain,
                    d.tx_hash,
                    d.event_index,
                    d.ts,
                    d.kind,
                    d.asset,
                    d.qty,
                    d.raw,
                    d.usd_price,
                    d.pricing_policy,
                )
                for d in drafts
            ],
        )
    await conn.execute("update public.wallets set sync_read = $2 where id = $1", wallet["id"], read)


async def _sync_solana(db: Database, sources: Sources, wallet: asyncpg.Record) -> tuple[str | None, int]:
    if sources.helius is None:
        raise IngestError("not_configured")
    address, stop_at = wallet["address"], wallet["sync_cursor"]
    before: str | None = None
    newest: str | None = None
    read = 0
    while read < sources.max_transactions:
        page = await sources.helius.transactions(address, before)
        if not page:
            break
        drafts: list[EventDraft] = []
        reached_known = False
        for tx in page:
            signature = tx.get("signature")
            if newest is None and isinstance(signature, str):
                newest = signature
            # na releitura, parar na transação mais nova já guardada
            if stop_at and signature == stop_at:
                reached_known = True
                break
            drafts.extend(solana.normalize_transaction(tx, address))
            read += 1
        async with db.service() as conn:
            await _store(conn, wallet, drafts, read)
        if reached_known or len(page) < PAGE_LIMIT:
            break
        before = str(page[-1].get("signature"))
        # folga para o limite de requisições por segundo do plano
        await asyncio.sleep(0.15)

    await _fill_symbols(db, sources.helius)
    return newest or stop_at, read


async def _fill_symbols(db: Database, helius: HeliusClient) -> None:
    async with db.service() as conn:
        missing = [
            r["asset"]
            for r in await conn.fetch(
                "select asset from public.assets where chain = 'solana' and symbol is null limit 300"
            )
        ]
    if not missing:
        return
    try:
        metadata = await helius.token_metadata(missing)
    except IngestError:
        return
    async with db.service() as conn:
        await conn.executemany(
            "update public.assets set symbol = coalesce(symbol, $2), decimals = coalesce(decimals, $3) "
            "where chain = 'solana' and asset = $1",
            [(mint, symbol, decimals) for mint, (symbol, decimals) in metadata.items()],
        )


def _split_cursor(cursor: str | None) -> tuple[int, int, int]:
    """fills:funding:extrato. Cursor antigo, de antes de o extrato ser lido, tem só as duas
    primeiras partes: o extrato começa do zero e é lido inteiro na próxima sincronização."""
    try:
        parts = [int(part) for part in (cursor or "0:0:0").split(":")]
    except ValueError:
        return 0, 0, 0
    if len(parts) not in (2, 3):
        return 0, 0, 0
    return parts[0], parts[1], parts[2] if len(parts) == 3 else 0


async def _sync_hyperliquid(db: Database, sources: Sources, wallet: asyncpg.Record) -> tuple[str, int]:
    address = wallet["address"]
    fills_from, funding_from, ledger_from = _split_cursor(wallet["sync_cursor"])
    read = 0

    async def drain(fetch: Any, normalize: Any, start: int, page_size: int) -> int:
        nonlocal read
        while read < sources.max_transactions:
            page = await fetch(address, start)
            drafts: list[EventDraft] = []
            for entry in page:
                drafts.extend(normalize(entry))
            read += len(page)
            async with db.service() as conn:
                await _store(conn, wallet, drafts, read)
            times = [e["time"] for e in page if isinstance(e.get("time"), int)]
            if times:
                start = max(times) + 1
            if len(page) < page_size:
                break
            await asyncio.sleep(0.3)
        return start

    fills_next = await drain(sources.hyperliquid.fills, hyperliquid.normalize_fill, fills_from, HL_FILLS_PAGE)
    funding_next = await drain(
        sources.hyperliquid.funding, hyperliquid.normalize_funding, funding_from, HL_FUNDING_PAGE
    )
    # depósitos, saques, transferências de token, airdrops e staking (B17)
    ledger_next = await drain(
        sources.hyperliquid.ledger,
        lambda entry: hyperliquid.normalize_ledger(entry, address),
        ledger_from,
        HL_LEDGER_PAGE,
    )
    await _fill_hyperliquid_symbols(db, sources.hyperliquid)
    return f"{fills_next}:{funding_next}:{ledger_next}", read


async def _fill_hyperliquid_symbols(db: Database, client: HyperliquidClient) -> None:
    async with db.service() as conn:
        missing = [
            r["asset"]
            for r in await conn.fetch(
                "select asset from public.assets "
                "where chain = 'hyperliquid' and symbol is null and asset like '@%' limit 300"
            )
        ]
    if not missing:
        return
    names = await client.spot_meta_names()
    updates = [
        (asset, names[asset], is_stable("hyperliquid", asset, names[asset]))
        for asset in missing
        if asset in names
    ]
    if not updates:
        return
    async with db.service() as conn:
        await conn.executemany(
            "update public.assets set symbol = $2, is_stable = is_stable or $3 "
            "where chain = 'hyperliquid' and asset = $1",
            updates,
        )


async def sync_wallet(db: Database, sources: Sources, wallet_id: UUID) -> UUID | None:
    """Lê uma carteira. Devolve o dono, ou None se a carteira não existe mais."""
    async with db.service() as conn:
        wallet = await conn.fetchrow(
            "update public.wallets set status = 'syncing', sync_error = null, sync_read = 0, "
            "sync_total = null, sync_started_at = now() where id = $1 "
            "returning id, user_id, chain, address, sync_cursor",
            wallet_id,
        )
    if wallet is None:
        return None
    # registro da leitura em ingestion_runs: período coberto, situação e limitações
    async with db.service() as conn:
        run_id = await conn.fetchval(
            "insert into public.ingestion_runs (wallet_id, user_id, chain, status) "
            "values ($1, $2, $3, 'running') returning id",
            wallet["id"],
            wallet["user_id"],
            wallet["chain"],
        )

    try:
        if wallet["chain"] == "solana":
            cursor, read = await _sync_solana(db, sources, wallet)
        else:
            cursor, read = await _sync_hyperliquid(db, sources, wallet)
    except IngestError as exc:
        await _fail(db, wallet_id, exc.code, run_id)
        return UUID(str(wallet["user_id"]))
    except Exception:
        log.exception("sincronização falhou", wallet_id=str(wallet_id))
        await _fail(db, wallet_id, "unexpected", run_id)
        return UUID(str(wallet["user_id"]))

    # parou no teto de transações: o que é mais antigo ficou de fora do cálculo
    truncated = read >= sources.max_transactions

    async with db.service() as conn:
        await conn.execute(
            """
            update public.wallets
               set status = case when exists (select 1 from public.events e where e.wallet_id = $1)
                                 then 'synced' else 'empty' end,
                   sync_cursor = $2, sync_read = $3, sync_total = $3, last_synced_at = now()
             where id = $1
            """,
            wallet_id,
            cursor,
            read,
        )
        await conn.execute(
            """
            update public.ingestion_runs r
               set status = $2, finished_at = now(), cursor_data = $3, limitations = $4,
                   covered_from = (select min(ts) from public.events where wallet_id = $5),
                   covered_to = (select max(ts) from public.events where wallet_id = $5)
             where r.id = $1
            """,
            run_id,
            "partial" if truncated else "complete",
            {"cursor": cursor, "read": read},
            f"truncated:{sources.max_transactions}" if truncated else None,
            wallet_id,
        )
    return UUID(str(wallet["user_id"]))


async def _fail(db: Database, wallet_id: UUID, code: str, run_id: UUID | None = None) -> None:
    async with db.service() as conn:
        await conn.execute(
            "update public.wallets set status = 'error', sync_error = $2 where id = $1", wallet_id, code
        )
        if run_id is not None:
            await conn.execute(
                "update public.ingestion_runs set status = 'failed', finished_at = now(), "
                "limitations = $2 where id = $1",
                run_id,
                f"failed:{code}",
            )


async def price_and_finish(db: Database, sources: Sources, user_id: UUID) -> dict[str, int]:
    """Cota os eventos do usuário e marca o primeiro acesso como concluído."""
    gecko = CoinGeckoClient(sources.http, sources.coingecko_key)
    async with db.service() as conn:
        summary = await price_user_events(conn, sources.http, gecko, user_id)
    async with db.service() as conn:
        await conn.execute(
            """
            update public.profiles set onboarded_at = now()
             where id = $1 and onboarded_at is null
               and exists (select 1 from public.wallets w where w.user_id = $1)
               and not exists (select 1 from public.wallets w
                                where w.user_id = $1 and w.status in ('pending', 'syncing'))
            """,
            user_id,
        )
    return summary
