"""Recotação periódica dos eventos que ficaram sem preço."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import fakeredis.aioredis
import httpx
import respx

from orbix.db import Database
from orbix.ingest.models import USDC_MINT
from orbix.prices.ptax import BRT, _has_weekday_after
from orbix.queue import active_key
from orbix.worker import reprice_pending
from tests.conftest import wallet_login
from tests.helpers import add_swap, wallet_of
from tests.test_sync import FakeHelius, sources


@respx.mock
async def test_reprice_fills_missing_values_and_skips_active_users(
    client: httpx.AsyncClient,
    admin: asyncpg.Connection,
    worker_db: Database,
    redis: fakeredis.aioredis.FakeRedis,
    http_out: httpx.AsyncClient,
) -> None:
    alice = (await wallet_login(client))["user"]["id"]
    bob = (await wallet_login(client))["user"]["id"]
    ts = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    for user in (alice, bob):
        # o lado USDC tem preço em dólar, mas o valor em reais ficou faltando (PTAX fora do ar)
        await add_swap(
            admin, user, await wallet_of(admin, user), ("SOL", "1", None), (USDC_MINT, "120", None), ts=ts
        )
    await admin.execute(
        "update public.events set usd_price = 1, pricing_policy = 'stable' where asset = $1", USDC_MINT
    )
    await redis.set(active_key(bob), 1)

    respx.get(url__startswith="https://olinda.bcb.gov.br/").respond(
        json={
            "value": [
                {"cotacaoCompra": 5.19, "cotacaoVenda": 5.2, "dataHoraCotacao": "2026-09-15 13:03:18.656"},
            ]
        }
    )
    respx.get(url__startswith="https://api.coingecko.com/").respond(json={"prices": []})

    ctx = {"db": worker_db, "redis": redis, "sources": sources(http_out, FakeHelius([]))}
    await reprice_pending(ctx)

    values = {
        str(r["user_id"]): r["brl_value"]
        for r in await admin.fetch("select user_id, brl_value from public.events where asset = $1", USDC_MINT)
    }
    assert values[alice] == Decimal("120") * Decimal("5.2")
    assert values[bob] is None  # leitura em andamento: fica para a própria leitura


@respx.mock
async def test_event_priced_with_the_previous_day_rate_gets_the_rate_of_its_own_day(
    client: httpx.AsyncClient,
    admin: asyncpg.Connection,
    worker_db: Database,
    redis: fakeredis.aioredis.FakeRedis,
    http_out: httpx.AsyncClient,
) -> None:
    """Entrada lida antes de a PTAX do dia existir na tabela: ficou com a da véspera e nada a
    corrigia depois (relatório de testes, B15)."""
    user = (await wallet_login(client))["user"]["id"]
    wallet = await wallet_of(admin, user)
    ts = datetime.now(UTC) - timedelta(days=2)
    day = ts.astimezone(BRT).date()
    await add_swap(admin, user, wallet, ("SOL", "1", "500"), (USDC_MINT, "100", "500"), ts=ts)
    await add_swap(admin, user, wallet, ("SOL", "2", "999"), (USDC_MINT, "200", "999"), ts=ts)
    await admin.execute("delete from public.fx_rates")
    await admin.execute(
        "update public.events set usd_price = 1, ptax = 5.0, ptax_date = $1 where asset = $2",
        day - timedelta(days=1),
        USDC_MINT,
    )
    # preço informado pelo usuário não é refeito
    await admin.execute(
        "update public.events set pricing_policy = 'manual' where asset = $1 and qty = 200", USDC_MINT
    )
    respx.get(url__startswith="https://olinda.bcb.gov.br/").respond(
        json={
            "value": [{"cotacaoCompra": 5.19, "cotacaoVenda": 5.2, "dataHoraCotacao": f"{day} 13:03:18.656"}]
        }
    )
    await reprice_pending({"db": worker_db, "redis": redis, "sources": sources(http_out, FakeHelius([]))})

    rows = {
        r["qty"]: r
        for r in await admin.fetch(
            "select qty, ptax, ptax_date, brl_value from public.events where asset = $1", USDC_MINT
        )
    }
    fixed, manual = rows[Decimal(100)], rows[Decimal(200)]
    assert (fixed["ptax"], fixed["ptax_date"], fixed["brl_value"]) == (Decimal("5.2"), day, Decimal(520))
    assert (manual["ptax"], manual["brl_value"]) == (Decimal("5.0"), Decimal(999))


def test_rates_are_fetched_again_when_a_weekday_is_missing() -> None:
    friday = datetime(2026, 10, 2).date()
    assert not _has_weekday_after(friday, friday + timedelta(days=2))  # sábado e domingo: nada a buscar
    assert _has_weekday_after(friday, friday + timedelta(days=3))  # segunda: pode ter PTAX nova
