"""Leitura de carteira e cotação, com as fontes externas simuladas e o banco real (papel orbix_worker)."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import asyncpg
import fakeredis.aioredis
import httpx
import respx

from orbix.db import Database
from orbix.ingest.hyperliquid import HyperliquidClient
from orbix.ingest.models import USDC_MINT, IngestError
from orbix.ingest.sync import Sources, price_and_finish, sync_wallet
from orbix.prices.service import Item, PriceSeries, _counter_leg_pass
from orbix.queue import active_key
from tests.conftest import FakeQueue, bearer, wallet_login
from tests.test_normalize import token_change, tx

TS = 1789500000  # 2026-09-15


class FakeHelius:
    def __init__(self, pages: list[list[dict[str, Any]]], fail: str | None = None) -> None:
        self.pages = pages
        self.fail = fail
        self.calls: list[str | None] = []

    async def transactions(self, address: str, before: str | None = None) -> list[dict[str, Any]]:
        self.calls.append(before)
        if self.fail:
            raise IngestError(self.fail)
        return self.pages.pop(0) if self.pages else []

    async def token_metadata(self, mints: list[str]) -> dict[str, tuple[str | None, int | None]]:
        return {mint: ("MEME", 6) for mint in mints}


def solana_tx(signature: str, wallet: str, **kw: Any) -> dict[str, Any]:
    data = tx(**kw)
    data.update(signature=signature, timestamp=TS, feePayer=wallet)
    data["accountData"][0]["account"] = wallet
    for account in data["accountData"][1:]:
        for change in account["tokenBalanceChanges"]:
            change["userAccount"] = wallet
    return data


def sources(http: httpx.AsyncClient, helius: Any) -> Sources:
    return Sources(
        http=http,
        helius=helius,
        hyperliquid=HyperliquidClient(http),
        coingecko_key=None,
        max_transactions=500,
    )


async def _login(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> tuple[dict[str, str], UUID, UUID, str]:
    session = await wallet_login(client)
    row = await admin.fetchrow("select id, address from public.wallets")
    return bearer(session["token"]), UUID(session["user"]["id"]), row["id"], row["address"]


async def test_sync_inserts_events_idempotently(
    client: httpx.AsyncClient, admin: asyncpg.Connection, worker_db: Database, http_out: httpx.AsyncClient
) -> None:
    _, user_id, wallet_id, address = await _login(client, admin)
    swap = solana_tx(
        "sigSwap", address, native=-1_500_005_000, changes=[token_change(address, USDC_MINT, "178500000", 6)]
    )
    received = solana_tx("sigIn", address, tx_type="TRANSFER", native=250_000_000, fee=0)

    helius = FakeHelius([[swap, received]])
    assert await sync_wallet(worker_db, sources(http_out, helius), wallet_id) == user_id
    events = await admin.fetch("select kind, asset, qty from public.events order by tx_hash, event_index")
    assert [(e["kind"], e["asset"]) for e in events] == [
        ("transfer_in", "SOL"),
        ("swap_out", "SOL"),
        ("swap_in", USDC_MINT),
        ("fee", "SOL"),
    ]
    wallet = await admin.fetchrow("select * from public.wallets where id = $1", wallet_id)
    assert (wallet["status"], wallet["sync_read"], wallet["sync_total"], wallet["sync_cursor"]) == (
        "synced",
        2,
        2,
        "sigSwap",
    )
    assert wallet["last_synced_at"] is not None
    assert await admin.fetchval("select is_stable from public.assets where asset = $1", USDC_MINT) is True

    # ler de novo as mesmas transações não duplica nada
    await admin.execute("update public.wallets set sync_cursor = null")
    await sync_wallet(worker_db, sources(http_out, FakeHelius([[swap, received]])), wallet_id)
    assert await admin.fetchval("select count(*) from public.events") == 4


async def test_resync_stops_at_last_known_transaction(
    client: httpx.AsyncClient, admin: asyncpg.Connection, worker_db: Database, http_out: httpx.AsyncClient
) -> None:
    _, _, wallet_id, address = await _login(client, admin)
    old = solana_tx("sigOld", address, tx_type="TRANSFER", native=100_000_000, fee=0)
    await sync_wallet(worker_db, sources(http_out, FakeHelius([[old]])), wallet_id)

    new = solana_tx("sigNew", address, tx_type="TRANSFER", native=300_000_000, fee=0)
    helius = FakeHelius([[new, old]])
    await sync_wallet(worker_db, sources(http_out, helius), wallet_id)
    assert helius.calls == [None]  # parou na transação conhecida, sem paginar
    assert await admin.fetchval("select count(*) from public.events") == 2
    assert await admin.fetchval("select sync_cursor from public.wallets") == "sigNew"


async def test_empty_wallet_and_source_failure(
    client: httpx.AsyncClient, admin: asyncpg.Connection, worker_db: Database, http_out: httpx.AsyncClient
) -> None:
    headers, _, wallet_id, _ = await _login(client, admin)
    await sync_wallet(worker_db, sources(http_out, FakeHelius([])), wallet_id)
    assert await admin.fetchval("select status from public.wallets") == "empty"

    await sync_wallet(worker_db, sources(http_out, FakeHelius([], fail="source_unavailable")), wallet_id)
    wallet = (await client.get("/api/wallets", headers=headers)).json()[0]
    assert wallet["status"] == "error"
    assert wallet["error"] == "A fonte de dados não respondeu. Tente sincronizar de novo."

    await sync_wallet(worker_db, sources(http_out, None), wallet_id)
    assert await admin.fetchval("select sync_error from public.wallets") == "not_configured"


@respx.mock
async def test_hyperliquid_sync(
    client: httpx.AsyncClient, admin: asyncpg.Connection, worker_db: Database, http_out: httpx.AsyncClient
) -> None:
    headers, _, _, _ = await _login(client, admin)
    added = (
        await client.post(
            "/api/wallets", json={"network": "hyperliquid", "address": "0x" + "a" * 40}, headers=headers
        )
    ).json()
    fill = {
        "coin": "HYPE",
        "px": "42.5",
        "sz": "10",
        "side": "A",
        "time": TS * 1000,
        "dir": "Close Long",
        "closedPnl": "37.25",
        "hash": "0xabc",
        "tid": 123,
        "fee": "0.19",
        "feeToken": "USDC",
    }
    funding = {
        "time": TS * 1000 + 5,
        "hash": "0x0",
        "delta": {"type": "funding", "coin": "HYPE", "usdc": "1.25"},
    }

    def info(request: httpx.Request) -> httpx.Response:
        kind = __import__("json").loads(request.content)["type"]
        return httpx.Response(200, json=[fill] if kind == "userFillsByTime" else [funding])

    respx.post("https://api.hyperliquid.xyz/info").mock(side_effect=info)
    await sync_wallet(worker_db, sources(http_out, None), UUID(added["id"]))
    events = await admin.fetch(
        "select kind, asset, usd_price, pricing_policy from public.events where chain = 'hyperliquid' order by ts"
    )
    assert [(e["kind"], e["asset"], e["usd_price"]) for e in events] == [
        ("perp_fill", "HYPE", Decimal("42.5")),
        ("funding", "USDC", Decimal("1")),
    ]
    wallet = await admin.fetchrow(
        "select status, sync_cursor from public.wallets where id = $1", UUID(added["id"])
    )
    assert wallet["status"] == "synced"
    assert wallet["sync_cursor"] == f"{TS * 1000 + 1}:{TS * 1000 + 6}"


@respx.mock
async def test_pricing_fills_brl_values_and_finishes_onboarding(
    client: httpx.AsyncClient, admin: asyncpg.Connection, worker_db: Database, http_out: httpx.AsyncClient
) -> None:
    headers, user_id, wallet_id, address = await _login(client, admin)
    swap = solana_tx(
        "sigSwap", address, native=-1_500_005_000, changes=[token_change(address, USDC_MINT, "178500000", 6)]
    )
    meme = "MEMEmint11111111111111111111111111111111111"
    meme_swap = solana_tx(
        "sigMeme",
        address,
        native=-5000,
        changes=[
            token_change(address, USDC_MINT, "-50000000", 6),
            token_change(address, meme, "9000000", 6),
        ],
    )
    src = sources(http_out, FakeHelius([[swap, meme_swap]]))
    await sync_wallet(worker_db, src, wallet_id)

    respx.get(url__startswith="https://olinda.bcb.gov.br/").respond(
        json={
            "value": [
                {"cotacaoCompra": 5.19, "cotacaoVenda": 5.2, "dataHoraCotacao": "2026-09-15 13:03:18.656"},
            ]
        }
    )
    start_ms = (TS - 7200) * 1000
    respx.get(url__startswith="https://api.coingecko.com/api/v3/coins/solana/market_chart/range").respond(
        json={"prices": [[start_ms + i * 3_600_000, 119.0] for i in range(5)]}
    )
    summary = await price_and_finish(worker_db, src, user_id)
    assert summary == {"events": 6, "priced": 6, "missing": 0}

    by = {(r["tx_hash"], r["kind"]): r for r in await admin.fetch("select * from public.events")}
    usdc_in = by[("sigSwap", "swap_in")]
    assert (usdc_in["pricing_policy"], usdc_in["usd_price"]) == ("stable", Decimal(1))
    assert usdc_in["brl_value"] == Decimal("178.5") * Decimal("5.2")
    sol_out = by[("sigSwap", "swap_out")]
    # SOL vale o que entrou em USDC: 178,5 / 1,5 = 119 por SOL
    assert (sol_out["pricing_policy"], sol_out["usd_price"]) == ("counter_leg", Decimal("119"))
    assert sol_out["ptax"] == Decimal("5.2")
    meme_in = by[("sigMeme", "swap_in")]
    assert meme_in["pricing_policy"] == "counter_leg"
    assert meme_in["brl_value"] == Decimal("50") * Decimal("5.2")
    assert by[("sigSwap", "fee")]["pricing_policy"] == "coingecko"

    assert await admin.fetchval("select onboarded_at is not null from public.profiles") is True
    assert (await client.get("/api/me", headers=headers)).json()["onboarded"] is True
    assert await admin.fetchval("select symbol from public.assets where asset = $1", meme) == "MEME"

    dashboard = (await client.get("/api/dashboard", headers=headers)).json()
    assert dashboard["month"] == "2026-09"
    assert dashboard["disposals"] == 2
    assert dashboard["missingPrices"] == 0


@respx.mock
async def test_manual_price_is_never_overwritten(
    client: httpx.AsyncClient, admin: asyncpg.Connection, worker_db: Database, http_out: httpx.AsyncClient
) -> None:
    _, user_id, wallet_id, address = await _login(client, admin)
    await sync_wallet(
        worker_db,
        sources(
            http_out,
            FakeHelius([[solana_tx("sigIn", address, tx_type="TRANSFER", native=1_000_000_000, fee=0)]]),
        ),
        wallet_id,
    )
    await admin.execute("update public.events set pricing_policy = 'manual', brl_value = null")
    respx.get(url__startswith="https://olinda.bcb.gov.br/").respond(json={"value": []})
    respx.get(url__startswith="https://api.coingecko.com/").respond(json={"prices": []})
    summary = await price_and_finish(worker_db, sources(http_out, None), user_id)
    assert summary["events"] == 0


async def test_sync_status_and_start(
    client: httpx.AsyncClient,
    admin: asyncpg.Connection,
    queue: FakeQueue,
    redis: fakeredis.aioredis.FakeRedis,
) -> None:
    headers, user_id, wallet_id, address = await _login(client, admin)
    idle = (await client.get("/api/ingest/status", headers=headers)).json()
    assert idle["state"] == "idle"
    assert [s["key"] for s in idle["steps"]] == ["verify", "solana", "prices", "dashboard"]
    assert idle["steps"][0] == {
        "key": "verify",
        "label": "Carteira verificada por assinatura",
        "state": "done",
    }

    started = (await client.post("/api/ingest", headers=headers)).json()
    assert started["state"] == "running"
    assert started["wallets"] == [
        {
            "walletId": str(wallet_id),
            "network": "solana",
            "address": address,
            "read": 0,
            "total": None,
            "state": "running",
            "detail": None,
            "error": None,
        }
    ]
    assert queue.jobs == [("sync_user", (str(user_id),), f"sync-user:{user_id}")]
    assert await redis.exists(active_key(user_id))

    # chamar de novo com a leitura em andamento reusa o mesmo job_id (a fila ignora a duplicata)
    await client.post("/api/ingest", headers=headers)
    assert {job_id for _, _, job_id in queue.jobs} == {f"sync-user:{user_id}"}

    # leitura terminou, cotação ainda rodando
    await admin.execute("update public.wallets set status = 'synced', sync_read = 40, sync_total = 40")
    pricing = (await client.get("/api/ingest/status", headers=headers)).json()
    assert pricing["state"] == "running"
    assert {s["key"]: s["state"] for s in pricing["steps"]} == {
        "verify": "done",
        "solana": "done",
        "prices": "running",
        "dashboard": "pending",
    }
    assert (pricing["read"], pricing["estimated"]) == (40, 40)

    await redis.delete(active_key(user_id))
    await admin.execute("update public.profiles set onboarded_at = now()")
    done = (await client.get("/api/ingest/status", headers=headers, params={})).json()
    assert done["state"] == "done"
    assert all(s["state"] == "done" for s in done["steps"])
    english = (await client.get("/api/ingest/status", headers={**headers, "Accept-Language": "en-US"})).json()
    assert english["steps"][1]["label"] == "Solana history read"


def test_counter_leg_needs_one_unknown_leg() -> None:
    now = datetime.now(UTC)
    wallet = UUID(int=1)

    def item(kind: str, asset: str, qty: str, usd: str | None, tx_hash: str = "t") -> Item:
        return Item(
            UUID(int=hash((kind, asset, tx_hash)) % 10**9),
            wallet,
            "solana",
            tx_hash,
            now,
            kind,
            asset,
            Decimal(qty),
            Decimal(usd) if usd else None,
            None,
        )

    simple = [item("swap_out", "MEME", "100", None), item("swap_in", "USDC", "50", "1")]
    assert _counter_leg_pass(simple) == 1
    assert simple[0].usd_price == Decimal("0.5")

    two_unknown = [
        item("swap_out", "A", "1", None),
        item("swap_out", "B", "1", None),
        item("swap_in", "USDC", "50", "1"),
    ]
    assert _counter_leg_pass(two_unknown) == 0

    lone = [item("transfer_in", "MEME", "100", None)]
    assert _counter_leg_pass(lone) == 0


def test_price_series_picks_nearest_within_tolerance() -> None:
    from datetime import timedelta

    base = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    series = PriceSeries(
        [(base, Decimal(100)), (base + timedelta(hours=1), Decimal(110))], timedelta(hours=3)
    )
    assert series.at(base + timedelta(minutes=20)) == (base, Decimal(100))
    assert series.at(base + timedelta(minutes=50)) == (base + timedelta(hours=1), Decimal(110))
    assert series.at(base + timedelta(hours=5)) is None
    assert PriceSeries([], timedelta(hours=3)).at(base) is None
