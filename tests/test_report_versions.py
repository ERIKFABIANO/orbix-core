"""Nova versão de um relatório final (relatório de testes de 09/10, B18).

O relatório de maio foi congelado antes de o extrato da Hyperliquid ser lido e ficou com o
custo errado. Relatório final não muda sozinho; quando os dados do mês mudam, o dono gera uma
nova versão. A anterior continua existindo e o link público dela continua abrindo.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import asyncpg
import fakeredis.aioredis
import httpx
import pytest
import respx
from solders.keypair import Keypair

from orbix.attest import memo
from orbix.attest.memo import MEMO_PROGRAM, memo_text
from orbix.db import Database
from orbix.ingest.models import USDC_MINT
from orbix.routers.reports import _signature, _version
from orbix.worker import attest_report_job, backfill_ledger
from tests.conftest import FakeQueue, FakeStorage, bearer, wallet_login
from tests.helpers import add_swap, seed_september
from tests.test_worker_attest import RPC, _ctx

MONTH = "2026-09"


class Chain:
    """RPC da Solana simulado: cada transação enviada ganha uma assinatura própria."""

    def __init__(self) -> None:
        self.sent = 0
        self.memos: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result: Any
        if body["method"] == "getLatestBlockhash":
            result = {"value": {"blockhash": "EkSnNWid2cvwEVnVx9aBqawnmiCNiDgp3gUdkDPTKN1N"}}
        elif body["method"] == "sendTransaction":
            self.sent += 1
            result = f"memoSignatureV{self.sent}"
        elif body["method"] == "getTransaction":
            text = self.memos.get(body["params"][0])
            result = {
                "meta": {"err": None},
                "transaction": {
                    "message": {"instructions": [{"programId": str(MEMO_PROGRAM), "parsed": text}]}
                },
            }
        else:
            result = {
                "value": [{"slot": 331508000 + self.sent, "confirmationStatus": "finalized", "err": None}]
            }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


async def _attest(
    worker_db: Database, redis: fakeredis.aioredis.FakeRedis, http: httpx.AsyncClient, queue: FakeQueue
) -> None:
    report_id = next(j for j in reversed(queue.jobs) if j[0] == "attest_report")[1][0]
    await attest_report_job(_ctx(worker_db, redis, http, str(Keypair())), report_id)


@respx.mock
async def test_new_version_of_a_final_report_keeps_the_old_one_verifiable(
    client: httpx.AsyncClient,
    admin: asyncpg.Connection,
    worker_db: Database,
    redis: fakeredis.aioredis.FakeRedis,
    http_out: httpx.AsyncClient,
    storage: FakeStorage,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(memo.asyncio, "sleep", no_sleep)
    chain = Chain()
    respx.post(RPC).mock(side_effect=chain.handler)

    session = await wallet_login(client)
    user_id, headers = session["user"]["id"], bearer(session["token"])
    seeded = await seed_september(admin, user_id)
    url = f"/api/report/{MONTH}"

    # rascunho não tem versão a refazer
    assert (await client.post(f"{url}/reissue", headers=headers)).json()["error"][
        "code"
    ] == "report_not_final"

    first = (await client.post(f"{url}/finalize", headers=headers)).json()
    assert (first["version"], first["outdated"], first["currentTotals"], first["previousVersions"]) == (
        1,
        False,
        None,
        [],
    )
    # a versão atual ainda não foi registrada na Solana: substituí-la agora a faria sumir
    pending = await client.post(f"{url}/reissue", headers=headers)
    assert (pending.status_code, pending.json()["error"]["code"]) == (409, "attestation_pending")

    await _attest(worker_db, redis, http_out, queue)
    v1 = await admin.fetchrow("select * from public.reports")
    chain.memos[v1["solana_sig"]] = memo_text(v1["sha256"])
    v1_key = f"reports/{user_id}/{MONTH}/orbix-declare-{MONTH}.csv"
    v1_bytes = storage.objects[v1_key]

    # nada mudou: não há o que atualizar (e nenhuma transação é gasta à toa)
    same = await client.post(f"{url}/reissue", headers=headers)
    assert (same.status_code, same.json()["error"]["code"]) == (409, "report_up_to_date")
    assert chain.sent == 1

    # os dados do mês mudam depois da finalização (carteira relida, por exemplo)
    await add_swap(
        admin,
        user_id,
        seeded["wallet_id"],
        ("SOL", "1", "1500"),
        (USDC_MINT, "288.46", "1500"),
        ts=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
    )
    stale = (await client.get(url, headers=headers)).json()
    # o relatório registrado não muda sozinho; a resposta avisa e traz o cálculo de hoje
    assert (stale["status"], stale["totals"]["gainBrl"], stale["outdated"]) == ("final", 2000.0, True)
    assert stale["currentTotals"]["gainBrl"] == 3000.0
    notice = stale["review"]["pendingReasons"][-1]
    assert "R$ 3.000,00" in notice and "R$ 2.000,00" in notice and "nova versão" in notice
    english = (await client.get(url, headers={**headers, "Accept-Language": "en"})).json()
    assert "Generate a new version" in english["review"]["pendingReasons"][-1]
    assert storage.objects[v1_key] == v1_bytes

    second = await client.post(f"{url}/reissue", headers=headers)
    assert second.status_code == 200, second.text
    body = second.json()
    assert (body["status"], body["version"], body["outdated"], body["currentTotals"]) == (
        "final",
        2,
        False,
        None,
    )
    assert body["totals"]["gainBrl"] == 3000.0 and len(body["rows"]) == 3
    assert body["attestation"] is None  # a versão nova ainda vai ser registrada
    assert not any("nova versão" in reason for reason in body["review"]["pendingReasons"])
    (old,) = body["previousVersions"]
    assert (old["version"], old["hash"], old["publicId"], old["txSignature"], old["slot"]) == (
        1,
        v1["sha256"],
        v1["public_id"],
        "memoSignatureV1",
        331508001,
    )

    # arquivo próprio para a versão nova; o da anterior continua intacto
    v2_key = f"reports/{user_id}/{MONTH}/orbix-declare-{MONTH}-v2.csv"
    v2 = await admin.fetchrow("select * from public.reports")
    assert storage.objects[v1_key] == v1_bytes
    assert v2["sha256"] == hashlib.sha256(storage.objects[v2_key]).hexdigest() != v1["sha256"]
    assert (v2["file_url"], v2["r2_object_key"], v2["status"]) == (v2_key, v2_key, "final")
    assert (v2["solana_sig"], v2["slot"], v2["attested_at"]) == (None, None, None)
    assert v2["public_id"] != v1["public_id"] and bytes(v2["salt"]) != bytes(v1["salt"])
    assert (v2["id"], float(v2["gains_brl"]), v2["events_count"]) == (v1["id"], 3000.0, 3)
    assert queue.jobs[-1] == ("attest_report", (str(v2["id"]),), f"attest:{v2['id']}")
    decripto = storage.objects[f"reports/{user_id}/{MONTH}/decripto-{MONTH}-v2.txt"].decode()
    assert v2["sha256"] in decripto and f"/v/{v2['public_id']}" in decripto
    link = (await client.get(f"{url}/csv", headers=headers)).json()
    assert link["filename"] == f"orbix-declare-{MONTH}-v2.csv" and v2_key in link["url"]
    assert (await client.post(f"{url}/decripto", headers=headers)).json()[
        "filename"
    ] == f"decripto-{MONTH}-v2.txt"

    # o público só vê a versão nova depois da transação; a anterior continua abrindo
    assert (await client.get(f"/api/verify/{v2['public_id']}")).status_code == 404
    earlier = (await client.get(f"/api/verify/{v1['public_id']}")).json()
    assert earlier == {
        "publicId": v1["public_id"],
        "description": "Relatório mensal · Setembro/2026 · titular ocultado · versão anterior",
        "month": MONTH,
        "hash": v1["sha256"],
        "txSignature": "memoSignatureV1",
        "slot": 331508001,
        "registeredAt": earlier["registeredAt"],
        "valid": True,
        "superseded": True,
    }

    await _attest(worker_db, redis, http_out, queue)
    chain.memos["memoSignatureV2"] = memo_text(v2["sha256"])
    attested = (await client.get(url, headers=headers)).json()
    assert attested["attestation"]["txSignature"] == "memoSignatureV2"
    assert attested["attestation"]["hash"] == v2["sha256"]
    current = (await client.get(f"/api/verify/{v2['public_id']}", headers={"Accept-Language": "en"})).json()
    assert (current["valid"], current["superseded"], current["hash"]) == (True, False, v2["sha256"])
    assert current["description"] == "Monthly report · September/2026 · holder hidden"

    # histórico imutável: uma linha por versão, cada uma com o próprio arquivo e transação
    versions = await admin.fetch("select * from public.report_versions order by revision")
    assert [(v["revision"], v["r2_object_key"], v["sha256"], v["solana_sig"]) for v in versions] == [
        (1, v1_key, v1["sha256"], "memoSignatureV1"),
        (2, v2_key, v2["sha256"], "memoSignatureV2"),
    ]
    assert versions[1]["snapshot"]["totals"]["gain_brl"] == 3000.0

    # terceira versão: as duas anteriores continuam verificáveis
    await add_swap(
        admin,
        user_id,
        seeded["wallet_id"],
        ("SOL", "1", "900"),
        (USDC_MINT, "173.07", "900"),
        ts=datetime(2026, 9, 27, 12, 0, tzinfo=UTC),
    )
    third = (await client.post(f"{url}/reissue", headers=headers)).json()
    assert (third["version"], [v["version"] for v in third["previousVersions"]]) == (3, [1, 2])
    assert f"reports/{user_id}/{MONTH}/orbix-declare-{MONTH}-v3.csv" in storage.objects
    for public_id, tx in ((v1["public_id"], "memoSignatureV1"), (v2["public_id"], "memoSignatureV2")):
        kept = (await client.get(f"/api/verify/{public_id}")).json()
        assert (kept["superseded"], kept["txSignature"], kept["valid"]) == (True, tx, True)
    assert (await client.get(f"/api/verify/{'0' * 8}")).status_code == 404

    # relatório de outra pessoa não é alcançado
    other = bearer((await wallet_login(client))["token"])
    assert (await client.post(f"{url}/reissue", headers=other)).json()["error"]["code"] == "report_not_final"
    assert (await client.get(url, headers=other)).json()["status"] == "draft"


@respx.mock
async def test_two_reissue_requests_at_once_create_one_version(
    client: httpx.AsyncClient,
    admin: asyncpg.Connection,
    worker_db: Database,
    redis: fakeredis.aioredis.FakeRedis,
    http_out: httpx.AsyncClient,
    storage: FakeStorage,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(memo.asyncio, "sleep", no_sleep)
    respx.post(RPC).mock(side_effect=Chain().handler)
    session = await wallet_login(client)
    user_id, headers = session["user"]["id"], bearer(session["token"])
    seeded = await seed_september(admin, user_id)
    await client.post(f"/api/report/{MONTH}/finalize", headers=headers)
    await _attest(worker_db, redis, http_out, queue)
    await add_swap(
        admin,
        user_id,
        seeded["wallet_id"],
        ("SOL", "1", "1500"),
        (USDC_MINT, "288.46", "1500"),
        ts=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
    )
    one, two = await asyncio.gather(
        client.post(f"/api/report/{MONTH}/reissue", headers=headers),
        client.post(f"/api/report/{MONTH}/reissue", headers=headers),
    )
    assert sorted((one.status_code, two.status_code)) == [200, 409]
    refused = one if one.status_code == 409 else two
    assert refused.json()["error"]["code"] in ("report_up_to_date", "attestation_pending")
    report = await admin.fetchrow("select file_url, data from public.reports")
    assert _version(report["file_url"]) == 2 and len(report["data"]["previous"]) == 1


async def test_reissue_refuses_what_finalize_refuses(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    session = await wallet_login(client)
    user_id, headers = session["user"]["id"], bearer(session["token"])
    seeded = await seed_september(admin, user_id)
    await client.post(f"/api/report/{MONTH}/finalize", headers=headers)
    await admin.execute("update public.reports set solana_sig = 'sig', slot = 1, attested_at = now()")
    # um evento novo sem preço: a nova versão sairia incompleta
    await add_swap(
        admin,
        user_id,
        seeded["wallet_id"],
        ("SOL", "1", None),
        ("BONKmintxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", "5", None),
        ts=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
    )
    refused = await client.post(f"/api/report/{MONTH}/reissue", headers=headers)
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "missing_prices")
    assert (await client.post("/api/report/setembro/reissue", headers=headers)).status_code == 422
    assert (await client.post(f"/api/report/{MONTH}/reissue")).status_code == 401
    assert await admin.fetchval("select solana_sig from public.reports") == "sig"


def test_signature_ignores_order_and_fields_added_later() -> None:
    old = {
        "totals": {"disposed_brl": 9000.0, "cost_brl": 7000.0, "gain_brl": 2000.0, "tax_brl": 0.0},
        "rows": [
            {"id": "a", "asset": "USDC → SOL", "value_brl": 5000.0, "cost_brl": 5000.0, "gain_brl": 0.0},
            {"id": "b", "asset": "SOL → USDC", "value_brl": 4000.0, "cost_brl": 2000.0, "gain_brl": 2000.0},
        ],
    }
    # mesmo conteúdo, outra ordem, ids novos e campos que a resposta ganhou depois
    new = {
        "totals": {**old["totals"]},  # type: ignore[dict-item]
        "previous": [{"version": 1}],
        "rows": [
            {**old["rows"][1], "id": "z", "cost_manual": False, "cost_unknown": False},  # type: ignore[index]
            {**old["rows"][0], "id": "y", "cost_manual": False},  # type: ignore[index]
        ],
    }
    assert _signature(old) == _signature(new)
    changed = {**new, "rows": [{**new["rows"][0], "cost_brl": 2000.01}, new["rows"][1]]}  # type: ignore[index]
    assert _signature(old) != _signature(changed)
    assert _signature(old) != _signature({**old, "rows": old["rows"][:1]})  # type: ignore[index]
    assert _signature({}) == _signature({"totals": {}, "rows": []})
    assert (_version(None), _version("r/u/2026-05/orbix-declare-2026-05.csv")) == (1, 1)
    assert (_version("r/u/2026-05/orbix-declare-2026-05-v2.csv"), _version("x/decripto-2026-05-v12.txt")) == (
        2,
        12,
    )


class _Jobs:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str, str | None]] = []

    async def enqueue_job(self, name: str, wallet_id: str, _job_id: str | None = None) -> None:
        self.jobs.append((name, wallet_id, _job_id))


async def test_worker_rereads_hyperliquid_wallets_read_before_the_ledger(
    client: httpx.AsyncClient, admin: asyncpg.Connection, worker_db: Database
) -> None:
    """B18: a carteira 2436 não tinha sido sincronizada depois do deploy, então os 296,66 HYPE
    recebidos por transferência não estavam na posição. O worker passa a reler sozinho."""
    # cada carteira numa conta: o plano gratuito limita o número de carteiras por pessoa
    ids: dict[str, UUID] = {}
    for name in ("a", "b", "c", "d", "e"):
        headers = bearer((await wallet_login(client))["token"])
        added = await client.post(
            "/api/wallets", json={"network": "hyperliquid", "address": "0x" + name * 40}, headers=headers
        )
        assert added.status_code == 200, added.text
        ids[name] = UUID(added.json()["id"])
    for name, cursor, status in (
        ("a", "1791000000000:1791000000001", "synced"),  # lida antes do extrato
        ("b", "1791000000000:1791000000001:1791000000002", "synced"),  # já tem o extrato
        ("c", "0:0", "empty"),  # só tinha depósito: aparecia vazia
        ("d", "5:6", "syncing"),  # leitura em andamento: não mexe
        ("e", "5:6", "error"),  # falhou na última vez: tenta de novo
    ):
        await admin.execute(
            "update public.wallets set sync_cursor = $2, status = $3 where id = $1", ids[name], cursor, status
        )
    # carteira Solana (a de login) tem outro formato de cursor
    await admin.execute(
        "update public.wallets set sync_cursor = 'sig', status = 'synced' where chain = 'solana'"
    )

    jobs = _Jobs()
    assert await backfill_ledger({"db": worker_db, "redis": jobs}) == 3
    assert sorted(jobs.jobs) == sorted(
        ("sync_wallet", str(ids[name]), f"sync-wallet:{ids[name]}") for name in ("a", "c", "e")
    )

    # falha ao enfileirar não derruba o worker
    class Broken:
        async def enqueue_job(self, *_: Any, **__: Any) -> None:
            raise ConnectionError("redis fora do ar")

    assert await backfill_ledger({"db": worker_db, "redis": Broken()}) == 0
