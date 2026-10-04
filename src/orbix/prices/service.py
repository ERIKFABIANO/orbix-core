"""Dá valor em reais a cada evento do usuário.

Ordem das fontes para o preço em dólar:
1. stablecoin vale 1;
2. a outra perna do mesmo swap (o que saiu vale o que entrou);
3. CoinGecko, primeiro SOL e depois os demais ativos, dentro de um teto de chamadas.

Depois multiplica pela PTAX de venda do dia (ou do último dia útil).
Preço informado à mão pelo usuário nunca é sobrescrito.
"""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg
import httpx
import structlog

from orbix.ingest.models import SOL, is_stable
from orbix.prices import ptax
from orbix.prices.coingecko import CoinGeckoClient, NotListed, RateLimited

log = structlog.get_logger()

SWAP_KINDS = ("swap_in", "swap_out")
HOUR_TOLERANCE = timedelta(hours=3)
DAY_TOLERANCE = timedelta(hours=36)
# o plano gratuito da CoinGecko só tem histórico do último ano
HISTORY_LIMIT = timedelta(days=364)


@dataclass
class Item:
    id: UUID
    wallet_id: UUID
    chain: str
    tx_hash: str
    ts: datetime
    kind: str
    asset: str
    qty: Decimal
    usd_price: Decimal | None
    policy: str | None


class PriceSeries:
    def __init__(self, points: list[tuple[datetime, Decimal]], tolerance: timedelta) -> None:
        points.sort()
        self._times = [t for t, _ in points]
        self._values = [v for _, v in points]
        self._tolerance = tolerance

    def at(self, moment: datetime) -> tuple[datetime, Decimal] | None:
        index = bisect_right(self._times, moment) - 1
        candidates = [i for i in (index, index + 1) if 0 <= i < len(self._times)]
        if not candidates:
            return None
        best = min(candidates, key=lambda i: abs(self._times[i] - moment))
        if abs(self._times[best] - moment) > self._tolerance:
            return None
        return self._times[best], self._values[best]


def _counter_leg_pass(items: list[Item]) -> int:
    """Num swap, se um lado inteiro tem preço e o outro tem uma única perna sem preço,
    essa perna vale o total do lado conhecido."""
    priced = 0
    groups: dict[tuple[UUID, str], list[Item]] = defaultdict(list)
    for item in items:
        if item.kind in SWAP_KINDS:
            groups[(item.wallet_id, item.tx_hash)].append(item)
    for legs in groups.values():
        for known_kind, unknown_kind in (("swap_in", "swap_out"), ("swap_out", "swap_in")):
            known = [leg for leg in legs if leg.kind == known_kind]
            unknown = [leg for leg in legs if leg.kind == unknown_kind and leg.usd_price is None]
            other_side = [leg for leg in legs if leg.kind == unknown_kind]
            if not known or any(leg.usd_price is None for leg in known):
                continue
            if len(unknown) != 1 or len(other_side) != 1 or unknown[0].qty <= 0:
                continue
            total = sum((leg.qty * leg.usd_price for leg in known if leg.usd_price is not None), Decimal(0))
            unknown[0].usd_price = total / unknown[0].qty
            unknown[0].policy = "counter_leg"
            priced += 1
    return priced


async def _load_cached(
    conn: asyncpg.Connection, chain: str, asset: str, start: datetime, end: datetime
) -> PriceSeries:
    rows = await conn.fetch(
        "select ts, usd_price, granularity from public.prices "
        "where chain = $1 and asset = $2 and ts between $3 and $4 order by ts",
        chain,
        asset,
        start - DAY_TOLERANCE,
        end + DAY_TOLERANCE,
    )
    daily = any(r["granularity"] == "day" for r in rows)
    return PriceSeries([(r["ts"], r["usd_price"]) for r in rows], DAY_TOLERANCE if daily else HOUR_TOLERANCE)


async def _coingecko_id(
    conn: asyncpg.Connection, gecko: CoinGeckoClient, chain: str, asset: str
) -> str | None:
    if chain != "solana":
        return None
    if asset == SOL:
        return "solana"
    row = await conn.fetchrow(
        "select coingecko_id, status, retry_after from public.assets where chain = $1 and asset = $2",
        chain,
        asset,
    )
    if row and row["coingecko_id"]:
        return str(row["coingecko_id"])
    if row and row["status"] != "ok" and row["retry_after"] and row["retry_after"] > datetime.now(UTC):
        return None
    try:
        coin_id, symbol = await gecko.resolve_solana_mint(asset)
    except NotListed:
        await conn.execute(
            "update public.assets set status = 'not_listed', attempts = attempts + 1, "
            "retry_after = now() + interval '7 days' where chain = $1 and asset = $2",
            chain,
            asset,
        )
        return None
    await conn.execute(
        "update public.assets set coingecko_id = $3, symbol = coalesce(symbol, $4), status = 'ok', "
        "resolved_at = now() where chain = $1 and asset = $2",
        chain,
        asset,
        coin_id,
        symbol,
    )
    return coin_id


async def _series_for(
    conn: asyncpg.Connection, gecko: CoinGeckoClient, chain: str, asset: str, moments: list[datetime]
) -> PriceSeries | None:
    start, end = min(moments), max(moments)
    cached = await _load_cached(conn, chain, asset, start, end)
    if all(cached.at(m) is not None for m in moments):
        return cached

    now = datetime.now(UTC)
    if end < now - HISTORY_LIMIT:
        return cached
    coin_id = await _coingecko_id(conn, gecko, chain, asset)
    if coin_id is None:
        return cached
    window_start = max(start, now - HISTORY_LIMIT) - timedelta(hours=2)
    window_end = min(end + timedelta(hours=2), now)
    try:
        points = await gecko.price_range(coin_id, window_start, window_end)
    except NotListed:
        return cached
    if not points:
        return cached
    # a CoinGecko devolve pontos por hora em janelas de até 90 dias e por dia acima disso
    granularity = "hour" if window_end - window_start <= timedelta(days=90) else "day"
    await conn.executemany(
        "insert into public.prices (chain, asset, ts, usd_price, granularity, source) "
        "values ($1, $2, $3, $4, $5, 'coingecko') on conflict (chain, asset, ts) do nothing",
        [(chain, asset, t, p, granularity) for t, p in points],
    )
    return await _load_cached(conn, chain, asset, start, end)


async def price_user_events(
    conn: asyncpg.Connection, http: httpx.AsyncClient, gecko: CoinGeckoClient, user_id: UUID
) -> dict[str, int]:
    """Preenche preço e valor em reais dos eventos sem valor. Devolve um resumo."""
    rows = await conn.fetch(
        "select id, wallet_id, chain, tx_hash, ts, kind, asset, qty, usd_price, pricing_policy "
        "from public.events where user_id = $1 and brl_value is null "
        "and pricing_policy is distinct from 'manual' order by ts",
        user_id,
    )
    items = [
        Item(
            r["id"],
            r["wallet_id"],
            r["chain"],
            r["tx_hash"],
            r["ts"],
            r["kind"],
            r["asset"],
            r["qty"],
            r["usd_price"],
            r["pricing_policy"],
        )
        for r in rows
    ]
    if not items:
        return {"events": 0, "priced": 0, "missing": 0}

    # 1. stablecoins
    for item in items:
        if item.usd_price is None and is_stable(item.chain, item.asset):
            item.usd_price, item.policy = Decimal(1), "stable"
    # 2. outra perna do swap
    _counter_leg_pass(items)

    # 3. CoinGecko: SOL primeiro, porque destrava a maioria dos swaps pela outra perna
    async def from_coingecko(chain: str, asset: str) -> None:
        pending = [i for i in items if i.usd_price is None and i.chain == chain and i.asset == asset]
        if not pending:
            return
        series = await _series_for(conn, gecko, chain, asset, [i.ts for i in pending])
        if series is None:
            return
        for item in pending:
            found = series.at(item.ts)
            if found is not None:
                item.usd_price, item.policy = found[1], "coingecko"

    try:
        await from_coingecko("solana", SOL)
        _counter_leg_pass(items)
        # só vale gastar chamada com ativo que apareceu em evento que entra no imposto
        wanted = sorted(
            {
                (i.chain, i.asset)
                for i in items
                if i.usd_price is None and i.chain == "solana" and i.kind in SWAP_KINDS
            }
        )
        for chain, asset in wanted:
            await from_coingecko(chain, asset)
        _counter_leg_pass(items)
    except RateLimited:
        log.warning("coingecko: limite atingido; eventos restantes ficam para a próxima rodada")

    # 4. PTAX e valor em reais
    priced = [i for i in items if i.usd_price is not None]
    updates: list[tuple[Any, ...]] = []
    if priced:
        start = ptax.brt_date(min(i.ts for i in priced))
        end = ptax.brt_date(max(i.ts for i in priced))
        await ptax.ensure_range(conn, http, start, end)
        table = await ptax.PtaxTable.load(conn, start, end)
        for item in priced:
            rate = table.lookup(ptax.brt_date(item.ts))
            if rate is None or item.usd_price is None:
                continue
            updates.append(
                (
                    item.id,
                    item.usd_price,
                    rate[1],
                    rate[0],
                    item.ts,
                    item.qty * item.usd_price * rate[1],
                    item.policy or "auto",
                )
            )
    if updates:
        await conn.executemany(
            "update public.events set usd_price = $2, ptax = $3, ptax_date = $4, price_ts = $5, "
            "brl_value = $6, pricing_policy = $7, review_reason = 'cotação automática' where id = $1",
            updates,
        )
    return {"events": len(items), "priced": len(updates), "missing": len(items) - len(updates)}
