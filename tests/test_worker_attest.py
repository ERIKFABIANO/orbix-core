"""Gravação do hash na Solana pelo worker, com o RPC simulado, e a trilha de auditoria do banco."""

import json
from datetime import UTC, datetime
from typing import Any

import asyncpg
import fakeredis.aioredis
import httpx
import pytest
import respx
from pydantic import SecretStr
from solders.keypair import Keypair

from orbix.attest import memo
from orbix.config import get_settings
from orbix.db import Database
from orbix.worker import attest_report_job
from tests.conftest import FakeQueue, bearer, wallet_login
from tests.helpers import add_swap, seed_september

RPC = "https://api.devnet.solana.com"


def _mock_rpc(sent: list[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result: Any
        if body["method"] == "getLatestBlockhash":
            result = {"value": {"blockhash": "EkSnNWid2cvwEVnVx9aBqawnmiCNiDgp3gUdkDPTKN1N"}}
        elif body["method"] == "sendTransaction":
            sent.append(body["params"][0])
            result = "5hN2memoSignature"
        else:
            result = {"value": [{"slot": 331508764, "confirmationStatus": "finalized", "err": None}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    respx.post(RPC).mock(side_effect=handler)


async def _finalized(
    client: httpx.AsyncClient, admin: asyncpg.Connection, queue: FakeQueue
) -> tuple[dict[str, str], str]:
    session = await wallet_login(client)
    await seed_september(admin, session["user"]["id"])
    headers = bearer(session["token"])
    assert (await client.post("/api/report/2026-09/decripto", headers=headers)).status_code == 200
    return headers, queue.jobs[-1][1][0]


def _ctx(
    worker_db: Database, redis: fakeredis.aioredis.FakeRedis, http: httpx.AsyncClient, secret: str | None
) -> dict[str, Any]:
    settings = get_settings().model_copy(
        update={"solana_memo_secret_key": SecretStr(secret) if secret else None}
    )
    return {"db": worker_db, "settings": settings, "redis": redis, "http": http, "job_try": 1}


@respx.mock
async def test_attest_records_signature_and_immutable_version(
    client: httpx.AsyncClient,
    admin: asyncpg.Connection,
    worker_db: Database,
    redis: fakeredis.aioredis.FakeRedis,
    http_out: httpx.AsyncClient,
    queue: FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(memo.asyncio, "sleep", no_sleep)
    headers, report_id = await _finalized(client, admin, queue)
    sent: list[str] = []
    _mock_rpc(sent)

    await attest_report_job(_ctx(worker_db, redis, http_out, str(Keypair())), report_id)

    report = await admin.fetchrow("select * from public.reports")
    assert (report["solana_sig"], report["slot"]) == ("5hN2memoSignature", 331508764)
    assert report["attested_at"] is not None and report["attest_error"] is None
    assert len(sent) == 1

    version = await admin.fetchrow("select * from public.report_versions")
    assert version["revision"] == 1
    assert (version["sha256"], version["solana_sig"]) == (report["sha256"], "5hN2memoSignature")
    assert version["r2_object_key"] == report["file_url"]
    assert version["rules_version"] == "br-2026.1"
    assert version["snapshot"]["totals"]["gain_brl"] == 2000.0

    # o relatório passa a mostrar a atestação para o dono
    detail = (await client.get("/api/report/2026-09", headers=headers)).json()
    assert detail["attestation"]["txSignature"] == "5hN2memoSignature"
    assert detail["attestation"]["hash"] == report["sha256"]

    # rodar de novo não envia outra transação nem cria outra versão
    await attest_report_job(_ctx(worker_db, redis, http_out, str(Keypair())), report_id)
    assert len(sent) == 1
    assert await admin.fetchval("select count(*) from public.report_versions") == 1

    # a versão é imutável: o banco recusa qualquer alteração
    with pytest.raises(asyncpg.PostgresError):
        await admin.execute("update public.report_versions set sha256 = $1", "0" * 64)


async def test_attest_without_key_marks_not_configured(
    client: httpx.AsyncClient,
    admin: asyncpg.Connection,
    worker_db: Database,
    redis: fakeredis.aioredis.FakeRedis,
    http_out: httpx.AsyncClient,
    queue: FakeQueue,
) -> None:
    _, report_id = await _finalized(client, admin, queue)
    await attest_report_job(_ctx(worker_db, redis, http_out, None), report_id)
    report = await admin.fetchrow("select solana_sig, attest_error from public.reports")
    assert (report["solana_sig"], report["attest_error"]) == (None, "not_configured")


@respx.mock
async def test_attest_retry_does_not_send_a_second_transaction(
    client: httpx.AsyncClient,
    admin: asyncpg.Connection,
    worker_db: Database,
    redis: fakeredis.aioredis.FakeRedis,
    http_out: httpx.AsyncClient,
    queue: FakeQueue,
) -> None:
    _, report_id = await _finalized(client, admin, queue)
    # uma tentativa anterior já enviou a transação e caiu antes de gravar
    await redis.set(f"attest:pending:{report_id}", "5hN2memoSignature")
    sent: list[str] = []
    _mock_rpc(sent)
    await attest_report_job(_ctx(worker_db, redis, http_out, str(Keypair())), report_id)
    assert sent == []
    assert await admin.fetchval("select solana_sig from public.reports") == "5hN2memoSignature"


async def test_manual_price_leaves_an_audit_trail(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    session = await wallet_login(client)
    user_id, headers = session["user"]["id"], bearer(session["token"])
    seeded = await seed_september(admin, user_id)
    unpriced = await add_swap(
        admin,
        user_id,
        seeded["wallet_id"],
        ("SOL", "1", None),
        ("X" * 44, "5", None),
        ts=datetime(2026, 9, 22, 12, 0, tzinfo=UTC),
    )
    assert (
        await client.put(f"/api/events/{unpriced}/price", json={"unitPriceBrl": 900}, headers=headers)
    ).status_code == 200

    reviews = await admin.fetch(
        "select * from public.event_reviews where event_id = $1 order by created_at", unpriced
    )
    assert reviews, "a alteração de preço precisa ficar registrada"
    first = reviews[0]
    assert first["reason"] == "preço informado pelo usuário"
    assert str(first["actor_user_id"]) == user_id  # quem fez foi o próprio usuário
    assert first["before_state"]["brl_value"] is None
    assert first["after_state"]["brl_value"] == 900
    assert (
        await admin.fetchval("select review_status from public.events where id = $1", unpriced) == "reviewed"
    )


async def test_user_cannot_change_event_identity_fields(
    client: httpx.AsyncClient, admin: asyncpg.Connection, db: Database
) -> None:
    session = await wallet_login(client)
    user_id = session["user"]["id"]
    seeded = await seed_september(admin, user_id)
    from uuid import UUID

    # mesmo com permissão de preço, o usuário não consegue mexer em quantidade, ativo ou dono
    for column, value in (
        ("qty", "999"),
        ("asset", "'USDC'"),
        ("user_id", "gen_random_uuid()"),
        ("kind", "'reward'"),
    ):
        with pytest.raises(asyncpg.PostgresError):
            async with db.as_user(UUID(user_id)) as conn:
                await conn.execute(
                    f"update public.events set {column} = {value} where id = $1", UUID(seeded["sale_id"])
                )
    assert await admin.fetchval("select qty from public.events where id = $1", UUID(seeded["sale_id"])) == 4
