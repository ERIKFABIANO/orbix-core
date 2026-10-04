"""Atalhos para popular o banco de teste com eventos já cotados."""

from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import count
from typing import Any

import asyncpg

from orbix.ingest.models import USDC_MINT

_tx = count(1)
SEPT = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)


async def wallet_of(admin: asyncpg.Connection, user_id: str) -> Any:
    return await admin.fetchval(
        "select id from public.wallets where user_id = $1 order by created_at limit 1", user_id
    )


async def add_event(
    admin: asyncpg.Connection,
    user_id: str,
    wallet_id: Any,
    kind: str,
    asset: str,
    qty: str,
    brl: str | None,
    *,
    ts: datetime = SEPT,
    tx: str | None = None,
    chain: str = "solana",
    raw: dict[str, Any] | None = None,
    index: int = 0,
    policy: str | None = "coingecko",
    ptax: str | None = "5.2",
) -> Any:
    tx = tx or f"tx{next(_tx)}"
    return await admin.fetchval(
        """
        insert into public.events (wallet_id, user_id, chain, tx_hash, event_index, ts, kind, asset, qty,
                                   brl_value, ptax, ptax_date, pricing_policy, raw, usd_price)
        values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15) returning id
        """,
        wallet_id,
        user_id,
        chain,
        tx,
        index,
        ts,
        kind,
        asset,
        Decimal(qty),
        Decimal(brl) if brl is not None else None,
        Decimal(ptax) if (ptax and brl is not None) else None,
        ts.date() if brl is not None else None,
        policy if brl is not None else None,
        raw or {"slot": 331508764},
        Decimal(1) if brl is not None else None,
    )


async def add_swap(
    admin: asyncpg.Connection,
    user_id: str,
    wallet_id: Any,
    sell: tuple[str, str, str | None],
    buy: tuple[str, str, str | None],
    *,
    ts: datetime = SEPT,
    tx: str | None = None,
) -> Any:
    """Devolve o id do evento de saída (o que identifica o swap nas rotas)."""
    tx = tx or f"swap{next(_tx)}"
    out_id = await add_event(admin, user_id, wallet_id, "swap_out", *sell, ts=ts, tx=tx, index=0)
    await add_event(admin, user_id, wallet_id, "swap_in", *buy, ts=ts, tx=tx, index=1)
    return out_id


async def seed_september(admin: asyncpg.Connection, user_id: str) -> dict[str, Any]:
    """Compra 10 SOL por R$ 5.000 e vende 4 por R$ 4.000: ganho de R$ 2.000 em setembro."""
    wallet_id = await wallet_of(admin, user_id)
    await admin.execute(
        "insert into public.assets (chain, asset, symbol, is_stable) values "
        "('solana', 'SOL', 'SOL', false), ('solana', $1, 'USDC', true) on conflict do nothing",
        USDC_MINT,
    )
    await add_swap(
        admin,
        user_id,
        wallet_id,
        (USDC_MINT, "1000", "5000"),
        ("SOL", "10", "5000"),
        ts=datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
    )
    sale = await add_swap(
        admin,
        user_id,
        wallet_id,
        ("SOL", "4", "4000"),
        (USDC_MINT, "769.23", "4000"),
        ts=datetime(2026, 9, 20, 18, 30, tzinfo=UTC),
        tx="5hN2saleSignature",
    )
    await admin.execute(
        "insert into public.fx_rates (date, ptax_buy, ptax_sell) values ($1, 5.19, 5.2) on conflict do nothing",
        date(2026, 9, 18),
    )
    return {"wallet_id": wallet_id, "sale_id": str(sale)}
