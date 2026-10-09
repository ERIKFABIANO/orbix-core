import base64
import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import asyncpg
import httpx
import respx

from orbix.attest.memo import MEMO_PROGRAM, memo_text
from orbix.ingest.models import USDC_MINT
from orbix.tax import service as tax
from tests.conftest import FakeQueue, FakeStorage, bearer, wallet_login
from tests.helpers import add_event, add_swap, seed_september

MONTH = "2026-09"


async def _user(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> tuple[dict[str, str], str, dict[str, object]]:
    session = await wallet_login(client)
    seeded = await seed_september(admin, session["user"]["id"])
    return bearer(session["token"]), session["user"]["id"], seeded


async def test_dashboard(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, _, _ = await _user(client, admin)
    body = (await client.get("/api/dashboard", headers=headers)).json()
    assert body["month"] == MONTH  # sem mês: o mais recente com dados
    assert body["volumeBrl"] == 9000.0  # 5.000 (USDC entregue) + 4.000 (SOL vendido)
    assert body["disposals"] == 2
    assert body["capitalGainBrl"] == 2000.0  # 4.000 - 4 x R$ 500
    assert body["estimatedTaxBrl"] == 0.0  # abaixo do limite: isento
    assert body["exemptionLimitBrl"] == 35000.0
    assert body["missingPrices"] == 0
    assert body["gainChangePct"] is None

    assert (
        await client.get("/api/dashboard", params={"month": "2026-13"}, headers=headers)
    ).status_code == 422
    empty = (await client.get("/api/dashboard", params={"month": "2025-01"}, headers=headers)).json()
    assert (empty["volumeBrl"], empty["disposals"]) == (0.0, 0)


async def test_events_list_and_shape(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, _, seeded = await _user(client, admin)
    events = (await client.get("/api/events", params={"month": MONTH}, headers=headers)).json()
    assert [e["asset"] for e in events] == ["SOL → USDC", "USDC → SOL"]  # mais recente primeiro
    sale = events[0]
    expected = {
        "id": seeded["sale_id"],
        "date": "2026-09-20T18:30:00Z",
        "network": "solana",
        "type": "swap",
        "asset": "SOL \u2192 USDC",
        "quantity": 4.0,
        "quantityAsset": "SOL",
        "valueBrl": 4000.0,
        "priceSource": "auto",
        "txHash": "5hN2saleSignature",
        "explorerUrl": "https://explorer.solana.com/tx/5hN2saleSignature",
    }
    assert {key: sale[key] for key in expected} == expected
    # detalhes para a revisão do evento
    assert sale["wallet"]["address"] == sale["wallet"]["address"] and sale["wallet"]["label"] == "Principal"
    assert (sale["unitPriceBrl"], sale["costBrl"], sale["gainBrl"]) == (1000.0, 2000.0, 2000.0)
    assert (sale["ptax"], sale["ptaxDate"], sale["ruleVersion"]) == (5.2, "2026-09-20", "br-2026.1")
    assert sale["priceProvider"] == "CoinGecko"
    assert sale["pendingReasons"] == [] and sale["reviewHistory"] == []


async def test_missing_price_and_manual_price(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, user_id, seeded = await _user(client, admin)
    unpriced = await add_swap(
        admin,
        user_id,
        seeded["wallet_id"],
        ("SOL", "1", None),
        ("MEMEmint11111111111111111111111111111111111", "500", None),
        ts=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    )
    events = (await client.get("/api/events", params={"month": MONTH}, headers=headers)).json()
    target = next(e for e in events if e["id"] == str(unpriced))
    assert target["valueBrl"] is None and target["priceSource"] is None
    assert (await client.get("/api/dashboard", headers=headers)).json()["missingPrices"] == 1

    for bad in ({"unitPriceBrl": 0}, {"unitPriceBrl": -5}, {"unitPriceBrl": "abc"}, {}):
        assert (
            await client.put(f"/api/events/{unpriced}/price", json=bad, headers=headers)
        ).status_code == 422

    updated = await client.put(
        f"/api/events/{unpriced}/price", json={"unitPriceBrl": 1100.5}, headers=headers
    )
    assert updated.status_code == 200
    assert updated.json()["valueBrl"] == 1100.5
    assert updated.json()["priceSource"] == "manual"
    assert (await client.get("/api/dashboard", headers=headers)).json()["missingPrices"] == 0
    # a PTAX do último dia útil anterior foi aplicada à linha
    assert await admin.fetchval("select ptax from public.events where id = $1", unpriced) == Decimal("5.2")

    row = next(
        r
        for r in (await client.get(f"/api/report/{MONTH}", headers=headers)).json()["rows"]
        if r["id"] == str(unpriced)
    )
    assert row["manualPrice"] is True


async def test_user_cannot_price_another_users_event(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    _, _, seeded = await _user(client, admin)
    mallory = await wallet_login(client)
    response = await client.put(
        f"/api/events/{seeded['sale_id']}/price", json={"unitPriceBrl": 1}, headers=bearer(mallory["token"])
    )
    assert response.status_code == 404
    assert (
        await admin.fetchval("select brl_value from public.events where id = $1", seeded["sale_id"]) == 4000
    )


async def test_report_list_and_draft_detail(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, _, _ = await _user(client, admin)
    reports = (await client.get("/api/reports", headers=headers)).json()
    assert reports == [
        {
            "month": MONTH,
            "status": "draft",
            "events": 2,
            "totalBrl": 9000.0,
            "updatedAt": "2026-09-20T18:30:00Z",
        }
    ]
    detail = (await client.get(f"/api/report/{MONTH}", headers=headers)).json()
    assert detail["status"] == "draft"
    assert detail["attestation"] is None
    assert detail["totals"] == {"disposedBrl": 9000.0, "costBrl": 7000.0, "gainBrl": 2000.0, "taxBrl": 0.0}
    sale = detail["rows"][-1]
    assert (sale["valueBrl"], sale["costBrl"], sale["gainBrl"], sale["ptax"]) == (4000.0, 2000.0, 2000.0, 5.2)
    assert (await client.get("/api/report/setembro", headers=headers)).status_code == 422


async def test_draft_csv_is_embedded_and_marked(
    client: httpx.AsyncClient, admin: asyncpg.Connection, storage: FakeStorage
) -> None:
    headers, _, _ = await _user(client, admin)
    link = (await client.get(f"/api/report/{MONTH}/csv", headers=headers)).json()
    assert link["filename"] == "orbix-declare-2026-09-rascunho.csv"
    prefix = "data:text/csv;charset=utf-8;base64,"
    assert link["url"].startswith(prefix)
    content = base64.b64decode(link["url"][len(prefix) :])
    assert content[:3] == bytes([0xEF, 0xBB, 0xBF])  # marca de UTF-8 para o Excel (B16)
    lines = content.decode("utf-8-sig").splitlines()
    assert lines[0] == (
        "data,tipo,ativo,quantidade,ptax,valor_brl,custo_brl,ganho_brl,preco_manual,"
        "taxas_brl,custo_desconhecido,rede,carteira,custo_informado"
    )
    sale = lines[2].split(",")
    assert sale[:9] == [
        "2026-09-20",
        "swap",
        "SOL → USDC",
        "4.00000000",
        "5.2000",
        "4000.00",
        "2000.00",
        "2000.00",
        "nao",
    ]
    # cada linha diz se o custo é desconhecido e de qual rede e carteira ela veio
    assert sale[10:12] == ["nao", "solana"] and "…" in sale[12] and sale[13] == "nao"
    # o rascunho termina no total: quem marca o arquivo como provisório é o nome dele
    assert lines[-1] == "total,,,,,9000.00,7000.00,2000.00,,,,,,"
    assert all(len(line.split(",")) == 14 for line in lines)
    assert storage.objects == {}  # rascunho não é guardado


async def test_finalize_freezes_csv_and_hash(
    client: httpx.AsyncClient, admin: asyncpg.Connection, storage: FakeStorage, queue: FakeQueue
) -> None:
    headers, user_id, _ = await _user(client, admin)
    response = await client.post(f"/api/report/{MONTH}/decripto", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["filename"] == "decripto-2026-09.txt"

    csv_key = f"reports/{user_id}/{MONTH}/orbix-declare-{MONTH}.csv"
    csv_bytes = storage.objects[csv_key]
    stored = await admin.fetchrow("select * from public.reports")
    # o hash guardado é exatamente o SHA-256 dos bytes do arquivo que o usuário baixa
    assert stored["sha256"] == hashlib.sha256(csv_bytes).hexdigest()
    assert stored["public_id"] == stored["sha256"][:8]
    assert stored["status"] == "final"
    last = csv_bytes.decode().splitlines()[-1]
    assert last.startswith("verificacao,") and len(last.split(",")[1]) == 64
    assert bytes.fromhex(last.split(",")[1]) == bytes(stored["salt"])
    assert queue.jobs[-1] == ("attest_report", (str(stored["id"]),), f"attest:{stored['id']}")

    decripto = storage.objects[f"reports/{user_id}/{MONTH}/decripto-{MONTH}.txt"].decode()
    assert stored["sha256"] in decripto and "Nao segue o leiaute oficial" in decripto

    detail = (await client.get(f"/api/report/{MONTH}", headers=headers)).json()
    assert detail["status"] == "final"
    assert detail["attestation"] is None  # ainda sem confirmação on-chain
    assert detail["totals"]["gainBrl"] == 2000.0
    csv_link = (await client.get(f"/api/report/{MONTH}/csv", headers=headers)).json()
    assert csv_link["url"].startswith(f"https://r2.test/{csv_key}")
    assert (await client.get("/api/reports", headers=headers)).json()[0]["status"] == "final"


async def test_final_report_does_not_change_or_refinalize(
    client: httpx.AsyncClient, admin: asyncpg.Connection, storage: FakeStorage, queue: FakeQueue
) -> None:
    headers, user_id, seeded = await _user(client, admin)
    await client.post(f"/api/report/{MONTH}/decripto", headers=headers)
    first_hash = await admin.fetchval("select sha256 from public.reports")
    first_csv = dict(storage.objects)

    # um evento novo no mesmo mês não altera o que foi congelado
    await add_swap(
        admin,
        user_id,
        seeded["wallet_id"],
        ("SOL", "1", "9999"),
        (USDC_MINT, "1", "9999"),
        ts=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
    )
    again = await client.post(f"/api/report/{MONTH}/decripto", headers=headers)
    assert again.status_code == 200
    assert await admin.fetchval("select sha256 from public.reports") == first_hash
    assert storage.objects == first_csv
    assert len([j for j in queue.jobs if j[0] == "attest_report"]) == 1
    assert (await client.get(f"/api/report/{MONTH}", headers=headers)).json()["totals"]["gainBrl"] == 2000.0
    # sem transação confirmada, consultar o relatório pede a gravação de novo (mesmo job_id)
    attest_jobs = [j for j in queue.jobs if j[0] == "attest_report"]
    assert len(attest_jobs) == 2 and attest_jobs[0][2] == attest_jobs[1][2]


async def test_finalize_refusals(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, user_id, seeded = await _user(client, admin)
    now = datetime.now(UTC)
    await add_swap(
        admin, user_id, seeded["wallet_id"], ("SOL", "1", "1000"), (USDC_MINT, "190", "1000"), ts=now
    )
    open_month = await client.post(f"/api/report/{tax.current_month()}/decripto", headers=headers)
    assert (open_month.status_code, open_month.json()["error"]["code"]) == (409, "month_open")

    empty = await client.post("/api/report/2025-01/decripto", headers=headers)
    assert (empty.status_code, empty.json()["error"]["code"]) == (409, "nothing_to_report")

    await add_swap(
        admin,
        user_id,
        seeded["wallet_id"],
        ("SOL", "1", None),
        ("X" * 44, "5", None),
        ts=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    )
    missing = await client.post(f"/api/report/{MONTH}/decripto", headers=headers)
    assert (missing.status_code, missing.json()["error"]["code"]) == (409, "missing_prices")
    assert "1 evento" in missing.json()["error"]["message"]


async def test_storage_down(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, _, _ = await _user(client, admin)
    client.app.state.storage = None  # type: ignore[attr-defined]
    response = await client.post(f"/api/report/{MONTH}/decripto", headers=headers)
    assert (response.status_code, response.json()["error"]["code"]) == (503, "storage_unavailable")


async def test_reports_are_isolated_between_users(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    headers, _, _ = await _user(client, admin)
    await client.post(f"/api/report/{MONTH}/decripto", headers=headers)
    bob = bearer((await wallet_login(client))["token"])
    assert (await client.get("/api/reports", headers=bob)).json() == []
    assert (await client.get(f"/api/report/{MONTH}", headers=bob)).json()["rows"] == []
    assert (await client.get("/api/events", params={"month": MONTH}, headers=bob)).json() == []
    assert (await client.get("/api/dashboard", headers=bob)).json()["volumeBrl"] == 0.0


async def test_perps_and_funding_in_report(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    session = await wallet_login(client)
    user_id, headers = session["user"]["id"], bearer(session["token"])
    added = await client.post(
        "/api/wallets", json={"network": "hyperliquid", "address": "0x" + "c" * 40}, headers=headers
    )
    # o banco exige que a rede do evento seja a da carteira
    wallet_id = UUID(added.json()["id"])
    hl = {"chain": "hyperliquid", "policy": "hyperliquid_fill"}
    await add_event(
        admin,
        user_id,
        wallet_id,
        "perp_fill",
        "HYPE",
        "10",
        "2080",
        raw={"closedPnl": "0", "fee": "0.2"},
        tx="0xa1",
        **hl,
    )
    await add_event(
        admin,
        user_id,
        wallet_id,
        "perp_fill",
        "HYPE",
        "10",
        "2288",
        raw={"closedPnl": "40", "fee": "0.3"},
        tx="0xa2",
        ts=datetime(2026, 9, 16, 15, 0, tzinfo=UTC),
        **hl,
    )
    await add_event(
        admin,
        user_id,
        wallet_id,
        "funding",
        "USDC",
        "2",
        "10.4",
        raw={"usdc": "2", "coin": "HYPE"},
        tx="funding-HYPE-1",
        ts=datetime(2026, 9, 17, 15, 0, tzinfo=UTC),
        **hl,
    )

    events = (await client.get("/api/events", params={"month": MONTH}, headers=headers)).json()
    assert [e["type"] for e in events] == ["funding", "perp", "perp"]
    assert events[1]["explorerUrl"] == "https://app.hyperliquid.xyz/explorer/tx/0xa2"
    assert events[0]["explorerUrl"].startswith("https://app.hyperliquid.xyz/explorer/address/")

    detail = (await client.get(f"/api/report/{MONTH}", headers=headers)).json()
    assert [r["type"] for r in detail["rows"]] == ["perp", "funding"]  # abertura não entra
    assert detail["rows"][0]["gainBrl"] == 205.4  # (40 - 0,5) x 5,2
    assert detail["totals"]["gainBrl"] == 215.8
    assert detail["totals"]["taxBrl"] == 32.37  # perps não entram na isenção


@respx.mock
async def test_public_verification(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    headers, _, _ = await _user(client, admin)
    await client.post(f"/api/report/{MONTH}/decripto", headers=headers)
    report = await admin.fetchrow("select id, sha256, public_id from public.reports")

    pending = await client.get(f"/api/verify/{report['public_id']}")
    assert pending.status_code == 404  # sem transação confirmada, não existe para o público

    await admin.execute(
        "update public.reports set solana_sig = '5hN2memoSig', slot = 331508764, attested_at = now() where id = $1",
        report["id"],
    )
    respx.post("https://api.devnet.solana.com").respond(
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "meta": {"err": None},
                "transaction": {
                    "message": {
                        "instructions": [
                            {
                                "programId": str(MEMO_PROGRAM),
                                "program": "spl-memo",
                                "parsed": memo_text(report["sha256"]),
                            }
                        ]
                    }
                },
            },
        }
    )
    body = (await client.get(f"/api/verify/{report['public_id']}")).json()
    assert body == {
        "publicId": report["public_id"],
        "description": "Relatório mensal · Setembro/2026 · titular ocultado",
        "month": MONTH,
        "hash": report["sha256"],
        "txSignature": "5hN2memoSig",
        "slot": 331508764,
        "registeredAt": body["registeredAt"],
        "valid": True,
        "superseded": False,
    }
    english = (
        await client.get(f"/api/verify/{report['public_id']}", headers={"Accept-Language": "en-US"})
    ).json()
    assert english["description"] == "Monthly report · September/2026 · holder hidden"

    detail = (await client.get(f"/api/report/{MONTH}", headers=headers)).json()
    assert detail["attestation"]["publicId"] == report["public_id"]
    assert detail["attestation"]["hash"] == report["sha256"]


@respx.mock
async def test_public_verification_detects_wrong_memo(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    headers, _, _ = await _user(client, admin)
    await client.post(f"/api/report/{MONTH}/decripto", headers=headers)
    public_id = await admin.fetchval("select public_id from public.reports")
    await admin.execute("update public.reports set solana_sig = 'sigX', slot = 1, attested_at = now()")
    respx.post("https://api.devnet.solana.com").respond(
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "meta": {"err": None},
                "transaction": {
                    "message": {
                        "instructions": [
                            {"programId": str(MEMO_PROGRAM), "parsed": "orbix-declare:v1:" + "0" * 64}
                        ]
                    }
                },
            },
        }
    )
    assert (await client.get(f"/api/verify/{public_id}")).json()["valid"] is False


async def test_public_verification_rejects_bad_ids(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    for bad in ("abc", "zzzzzzzz", "a3f9c27e'--", "A" * 100, "a3f9c27e"):
        assert (await client.get(f"/api/verify/{bad}")).status_code == 404
