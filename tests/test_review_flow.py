"""Revisão de eventos, finalização explícita, bloco de revisão do relatório e cobertura da leitura."""

from datetime import UTC, datetime
from uuid import UUID

import asyncpg
import httpx

from orbix.db import Database
from orbix.ingest.models import USDC_MINT
from orbix.ingest.sync import sync_wallet
from tests.conftest import FakeQueue, FakeStorage, bearer, wallet_login
from tests.helpers import add_event, add_swap, seed_september
from tests.test_normalize import token_change
from tests.test_sync import FakeHelius, solana_tx, sources

MONTH = "2026-09"


async def _user(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> tuple[dict[str, str], str, dict[str, object]]:
    session = await wallet_login(client)
    seeded = await seed_september(admin, session["user"]["id"])
    await admin.execute("update public.wallets set status = 'synced', last_synced_at = now()")
    return bearer(session["token"]), session["user"]["id"], seeded


async def _unpriced(admin: asyncpg.Connection, user_id: str, seeded: dict[str, object]) -> str:
    out_id = await add_swap(
        admin,
        user_id,
        seeded["wallet_id"],
        ("SOL", "2", None),
        ("MEMEmint11111111111111111111111111111111111", "500", None),
        ts=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    )
    return str(out_id)


async def test_audited_price_review_records_reason_and_evidence(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    headers, user_id, seeded = await _user(client, admin)
    event_id = await _unpriced(admin, user_id, seeded)

    before = next(
        e
        for e in (await client.get("/api/events", params={"month": MONTH}, headers=headers)).json()
        if e["id"] == event_id
    )
    assert before["valueBrl"] is None
    assert any("Nenhuma fonte de preço" in r for r in before["pendingReasons"])

    response = await client.put(
        f"/api/events/{event_id}/price",
        json={
            "unitPriceBrl": 610.5,
            "reason": "Preço da corretora",
            "evidence": "Print da Binance de 22/09",
            "confirmed": True,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    event = response.json()
    assert (event["valueBrl"], event["unitPriceBrl"], event["priceSource"]) == (1221.0, 610.5, "manual")
    assert event["priceProvider"] is None
    assert event["pendingReasons"] == []
    (review,) = event["reviewHistory"]
    assert review["reason"] == "Preço da corretora"
    assert review["evidence"] == "Print da Binance de 22/09"
    assert (review["previousPriceBrl"], review["newPriceBrl"]) == (None, 610.5)

    # uma única linha de auditoria, com quem fez e a evidência
    rows = await admin.fetch("select * from public.event_reviews where event_id = $1", UUID(event_id))
    assert len(rows) == 1
    assert str(rows[0]["actor_user_id"]) == user_id
    assert rows[0]["evidence"] == {"text": "Print da Binance de 22/09"}

    # uma segunda revisão entra no histórico, com o preço anterior
    await client.put(
        f"/api/events/{event_id}/price",
        json={"unitPriceBrl": 600, "reason": "Corrigido", "evidence": "Extrato", "confirmed": True},
        headers=headers,
    )
    listed = next(
        e
        for e in (await client.get("/api/events", params={"month": MONTH}, headers=headers)).json()
        if e["id"] == event_id
    )
    assert [(r["previousPriceBrl"], r["newPriceBrl"]) for r in listed["reviewHistory"]] == [
        (None, 610.5),
        (610.5, 600.0),
    ]


async def test_incomplete_review_is_refused(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, user_id, seeded = await _user(client, admin)
    event_id = await _unpriced(admin, user_id, seeded)
    for body in (
        {"unitPriceBrl": 10, "reason": "x"},
        {"unitPriceBrl": 10, "reason": "x", "evidence": "y"},
        {"unitPriceBrl": 10, "reason": "x", "evidence": "y", "confirmed": False},
        {"unitPriceBrl": 10, "reason": "", "evidence": "y", "confirmed": True},
    ):
        response = await client.put(f"/api/events/{event_id}/price", json=body, headers=headers)
        assert response.status_code == 422, body
    # sem campos de revisão continua aceito (compatível com o front atual)
    assert (
        await client.put(f"/api/events/{event_id}/price", json={"unitPriceBrl": 10}, headers=headers)
    ).status_code == 200


async def test_automatic_pricing_does_not_appear_as_user_review(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    headers, user_id, seeded = await _user(client, admin)
    event_id = await _unpriced(admin, user_id, seeded)
    await admin.execute(
        "update public.events set brl_value = 100, review_reason = 'cotação automática' where id = $1",
        UUID(event_id),
    )
    event = next(
        e
        for e in (await client.get("/api/events", params={"month": MONTH}, headers=headers)).json()
        if e["id"] == event_id
    )
    assert event["reviewHistory"] == []


async def test_dashboard_exemption_status(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, user_id, seeded = await _user(client, admin)
    assert (await client.get("/api/dashboard", headers=headers)).json()["exemptionStatus"] == "exempt"

    await _unpriced(admin, user_id, seeded)
    # com evento sem preço, o total pode mudar: o motor não decide a isenção
    assert (await client.get("/api/dashboard", headers=headers)).json()["exemptionStatus"] is None

    await admin.execute("delete from public.events where brl_value is null")
    await add_swap(
        admin,
        user_id,
        seeded["wallet_id"],
        ("SOL", "6", "40000"),
        (USDC_MINT, "7700", "40000"),
        ts=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
    )
    assert (await client.get("/api/dashboard", headers=headers)).json()["exemptionStatus"] == "taxable"


async def test_report_review_block(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, user_id, seeded = await _user(client, admin)
    # venda de um ativo cuja compra não está no histórico
    await add_event(
        admin,
        user_id,
        seeded["wallet_id"],
        "swap_out",
        "JUPmint1111111111111111111111111111111111111",
        "100",
        "500",
        tx="sellJup",
        ts=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )
    await add_event(
        admin,
        user_id,
        seeded["wallet_id"],
        "swap_in",
        USDC_MINT,
        "96",
        "500",
        tx="sellJup",
        ts=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        index=1,
    )
    review = (await client.get(f"/api/report/{MONTH}", headers=headers)).json()["review"]
    assert review["engineVersion"] == "br-2026.1"
    assert review["coverage"]["state"] == "complete"
    assert review["coverage"]["importedFrom"] == "2026-09-05"
    assert review["coverage"]["importedEvents"] == 6
    assert any("leiaute oficial" in item for item in review["limitations"])
    assert any("devnet" in item for item in review["limitations"])
    assert review["pendingReasons"] == []
    assert review["unsupportedOperations"] == []
    (item,) = review["reviewItems"]
    assert item["kind"] == "acquisition_cost" and "não encontrada" in item["label"]
    # o arquivo ainda não segue o leiaute oficial: nunca "pronto"
    assert review["decriptoReady"] is False

    english = (
        await client.get(f"/api/report/{MONTH}", headers={**headers, "Accept-Language": "en-US"})
    ).json()["review"]
    assert any("official filing layout" in item for item in english["limitations"])


async def test_coverage_partial_when_a_wallet_failed(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    headers, _, _ = await _user(client, admin)
    await admin.execute("update public.wallets set status = 'error'")
    review = (await client.get(f"/api/report/{MONTH}", headers=headers)).json()["review"]
    assert review["coverage"]["state"] == "partial"
    assert any("não está completa" in r for r in review["pendingReasons"])


async def test_explicit_finalize_route(
    client: httpx.AsyncClient, admin: asyncpg.Connection, storage: FakeStorage, queue: FakeQueue
) -> None:
    headers, _, _ = await _user(client, admin)
    response = await client.post(f"/api/report/{MONTH}/finalize", headers=headers)
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["status"] == "final"
    assert detail["totals"]["gainBrl"] == 2000.0
    assert detail["review"]["coverage"]["state"] == "complete"
    assert len(storage.objects) == 2
    assert any(job[0] == "attest_report" for job in queue.jobs)

    # finalizar de novo não muda nada; a DeCripto usa o relatório congelado
    first_hash = await admin.fetchval("select sha256 from public.reports")
    assert (await client.post(f"/api/report/{MONTH}/finalize", headers=headers)).json()["status"] == "final"
    assert await admin.fetchval("select sha256 from public.reports") == first_hash
    decripto = await client.post(f"/api/report/{MONTH}/decripto", headers=headers)
    assert decripto.json()["filename"] == "decripto-2026-09.txt"
    assert await admin.fetchval("select sha256 from public.reports") == first_hash


async def test_finalize_refuses_open_month(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, _, _ = await _user(client, admin)
    from orbix.tax import service as tax

    response = await client.post(f"/api/report/{tax.current_month()}/finalize", headers=headers)
    assert (response.status_code, response.json()["error"]["code"]) == (409, "month_open")


async def test_sync_records_ingestion_run(
    client: httpx.AsyncClient, admin: asyncpg.Connection, worker_db: Database, http_out: httpx.AsyncClient
) -> None:
    session = await wallet_login(client)
    wallet = await admin.fetchrow("select id, address from public.wallets")
    swap = solana_tx(
        "sigSwap",
        wallet["address"],
        native=-1_500_005_000,
        changes=[token_change(wallet["address"], USDC_MINT, "178500000", 6)],
    )
    await sync_wallet(worker_db, sources(http_out, FakeHelius([[swap]])), wallet["id"])
    run = await admin.fetchrow("select * from public.ingestion_runs")
    assert run["status"] == "complete"
    assert run["finished_at"] is not None and run["covered_from"] is not None
    assert run["cursor_data"] == {"cursor": "sigSwap", "read": 1}
    assert run["limitations"] is None

    # parar no teto de transações vira leitura parcial, com a limitação registrada
    many = [
        solana_tx(f"sig{i}", wallet["address"], tx_type="TRANSFER", native=1_000_000, fee=0) for i in range(3)
    ]
    await admin.execute("update public.wallets set sync_cursor = null")
    src = sources(http_out, FakeHelius([many]))
    src.max_transactions = 3
    await sync_wallet(worker_db, src, wallet["id"])
    latest = await admin.fetchrow("select * from public.ingestion_runs order by started_at desc limit 1")
    assert (latest["status"], latest["limitations"]) == ("partial", "truncated:3")

    headers = bearer(session["token"])
    review = (await client.get("/api/report/2026-09", headers=headers)).json()["review"]
    assert review["coverage"]["state"] == "partial"
    assert any("3 transações mais recentes" in item for item in review["limitations"])

    # falha da fonte vira registro de falha
    await sync_wallet(worker_db, sources(http_out, FakeHelius([], fail="source_unavailable")), wallet["id"])
    failed = await admin.fetchrow("select * from public.ingestion_runs order by started_at desc limit 1")
    assert (failed["status"], failed["limitations"]) == ("failed", "failed:source_unavailable")


async def test_acquisition_cost_review_resolves_unknown_cost(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    headers, user_id, seeded = await _user(client, admin)
    jup = "JUPmint1111111111111111111111111111111111111"
    sale = await add_event(
        admin,
        user_id,
        seeded["wallet_id"],
        "swap_out",
        jup,
        "100",
        "500",
        tx="sellJup",
        ts=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )
    await add_event(
        admin,
        user_id,
        seeded["wallet_id"],
        "swap_in",
        USDC_MINT,
        "96",
        "500",
        tx="sellJup",
        ts=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        index=1,
    )
    report = (await client.get(f"/api/report/{MONTH}", headers=headers)).json()
    assert len(report["review"]["reviewItems"]) == 1
    row = next(r for r in report["rows"] if r["id"] == str(sale))
    assert (row["costBrl"], row["gainBrl"], row["costUnknown"]) == (0.0, 500.0, True)

    for bad in (
        {"costBrl": 300},
        {"costBrl": -1, "reason": "x", "evidence": "y", "confirmed": True},
        {"costBrl": 300, "reason": "x", "evidence": "y", "confirmed": False},
    ):
        assert (await client.put(f"/api/events/{sale}/cost", json=bad, headers=headers)).status_code == 422

    response = await client.put(
        f"/api/events/{sale}/cost",
        json={
            "costBrl": 320,
            "reason": "Comprei na Binance em 2025",
            "evidence": "Extrato da corretora",
            "confirmed": True,
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    event = response.json()
    assert (event["costBrl"], event["gainBrl"], event["pendingReasons"]) == (320.0, 180.0, [])
    (review,) = event["reviewHistory"]
    assert (review["kind"], review["previousCostBrl"], review["newCostBrl"]) == ("cost", None, 320.0)
    assert review["evidence"] == "Extrato da corretora"

    report = (await client.get(f"/api/report/{MONTH}", headers=headers)).json()
    assert report["review"]["reviewItems"] == []
    row = next(r for r in report["rows"] if r["id"] == str(sale))
    assert (row["costBrl"], row["gainBrl"], row["costUnknown"], row["costManual"]) == (
        320.0,
        180.0,
        False,
        True,
    )


async def test_cost_review_only_on_own_sales(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, _user_id, seeded = await _user(client, admin)
    body = {"costBrl": 1, "reason": "x", "evidence": "y", "confirmed": True}
    # o evento de entrada de um swap não é uma venda
    buy_in = await admin.fetchval("select id from public.events where kind = 'swap_in' limit 1")
    assert (await client.put(f"/api/events/{buy_in}/cost", json=body, headers=headers)).status_code == 404
    mallory = bearer((await wallet_login(client))["token"])
    assert (
        await client.put(f"/api/events/{seeded['sale_id']}/cost", json=body, headers=mallory)
    ).status_code == 404
    assert (
        await admin.fetchval(
            "select cost_override_brl from public.events where id = $1", UUID(seeded["sale_id"])
        )
        is None
    )
