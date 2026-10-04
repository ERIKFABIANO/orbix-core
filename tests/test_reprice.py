"""Recotação periódica dos eventos que ficaram sem preço."""

from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
import fakeredis.aioredis
import httpx
import respx

from orbix.db import Database
from orbix.ingest.models import USDC_MINT
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
