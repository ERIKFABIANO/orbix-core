import asyncpg
import httpx

from tests.conftest import FakeMailer, FakeQueue, bearer, email_login, wallet_login

HL = "0x" + "aB" * 20


async def test_list_shows_login_wallet(client: httpx.AsyncClient) -> None:
    session = await wallet_login(client)
    wallets = (await client.get("/api/wallets", headers=bearer(session["token"]))).json()
    assert len(wallets) == 1
    wallet = wallets[0]
    assert wallet["network"] == "solana"
    assert wallet["isLogin"] is True
    assert wallet["verifiedAt"] is not None
    assert wallet["status"] == "pending"
    assert wallet["label"] == "Principal"
    assert set(wallet) == {
        "id",
        "network",
        "address",
        "label",
        "isLogin",
        "verifiedAt",
        "lastSyncAt",
        "status",
        "error",
    }


async def test_add_hyperliquid_wallet_starts_sync(client: httpx.AsyncClient, queue: FakeQueue) -> None:
    session = await wallet_login(client)
    response = await client.post(
        "/api/wallets",
        json={"network": "hyperliquid", "address": HL, "label": "Perps"},
        headers=bearer(session["token"]),
    )
    assert response.status_code == 200, response.text
    wallet = response.json()
    assert wallet["address"] == HL.lower()
    assert wallet["status"] == "syncing"
    assert wallet["isLogin"] is False
    assert wallet["verifiedAt"] is None
    assert queue.jobs == [("sync_wallet", (wallet["id"],), f"sync-wallet:{wallet['id']}")]


async def test_add_solana_wallet_by_address_is_unverified(
    client: httpx.AsyncClient, mailer: FakeMailer
) -> None:
    session = await email_login(client, mailer, "ana@exemplo.com")
    response = await client.post(
        "/api/wallets",
        json={"network": "solana", "address": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"},
        headers=bearer(session["token"]),
    )
    assert response.status_code == 200
    assert response.json()["verifiedAt"] is None
    assert response.json()["label"] == "Carteira Solana"
    me = (await client.get("/api/me", headers=bearer(session["token"]))).json()
    assert me["hasWallets"] is True


async def test_invalid_duplicate_and_wrong_network(client: httpx.AsyncClient) -> None:
    session = await wallet_login(client)
    headers = bearer(session["token"])
    cases = [
        {"network": "hyperliquid", "address": "0x123"},
        {"network": "hyperliquid", "address": "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"},
        {"network": "solana", "address": HL},
        {"network": "solana", "address": "<script>alert(1)</script>"},
    ]
    for body in cases:
        response = await client.post("/api/wallets", json=body, headers=headers)
        assert response.status_code == 422, body
        assert response.json()["error"]["code"] == "invalid_address"

    assert (
        await client.post("/api/wallets", json={"network": "ethereum", "address": HL}, headers=headers)
    ).status_code == 422
    assert (
        await client.post("/api/wallets", json={"network": "hyperliquid", "address": HL}, headers=headers)
    ).status_code == 200
    # mesma carteira com outra caixa: ainda é duplicata
    duplicate = await client.post(
        "/api/wallets",
        json={"network": "hyperliquid", "address": HL.upper().replace("0X", "0x")},
        headers=headers,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_wallet"


async def test_free_plan_limit(client: httpx.AsyncClient) -> None:
    session = await wallet_login(client)  # 1 carteira (login)
    headers = bearer(session["token"])
    for i in range(2):
        ok = await client.post(
            "/api/wallets", json={"network": "hyperliquid", "address": f"0x{i:040x}"}, headers=headers
        )
        assert ok.status_code == 200
    blocked = await client.post(
        "/api/wallets", json={"network": "hyperliquid", "address": "0x" + "f" * 40}, headers=headers
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "plan_limit"
    assert "3" in blocked.json()["error"]["message"]


async def test_user_cannot_set_system_fields(client: httpx.AsyncClient) -> None:
    session = await wallet_login(client)
    response = await client.post(
        "/api/wallets",
        json={"network": "hyperliquid", "address": HL, "verified": True, "isLogin": True, "userId": "x"},
        headers=bearer(session["token"]),
    )
    assert response.status_code == 422


async def test_remove_rules(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    session = await wallet_login(client)
    headers = bearer(session["token"])
    login_wallet = (await client.get("/api/wallets", headers=headers)).json()[0]
    blocked = await client.delete(f"/api/wallets/{login_wallet['id']}", headers=headers)
    assert (blocked.status_code, blocked.json()["error"]["code"]) == (409, "login_wallet")

    added = (
        await client.post("/api/wallets", json={"network": "hyperliquid", "address": HL}, headers=headers)
    ).json()
    busy = await client.delete(f"/api/wallets/{added['id']}", headers=headers)
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "sync_in_progress")

    await admin.execute("update public.wallets set status = 'synced' where id = $1", added["id"])
    assert (await client.delete(f"/api/wallets/{added['id']}", headers=headers)).status_code == 204
    assert (await client.delete(f"/api/wallets/{added['id']}", headers=headers)).status_code == 404
    assert (await client.delete("/api/wallets/nao-e-uuid", headers=headers)).status_code == 404


async def test_user_cannot_touch_another_users_wallet(
    client: httpx.AsyncClient, admin: asyncpg.Connection, queue: FakeQueue
) -> None:
    alice = await wallet_login(client)
    bob = await wallet_login(client)
    alice_wallet = (
        await client.post(
            "/api/wallets", json={"network": "hyperliquid", "address": HL}, headers=bearer(alice["token"])
        )
    ).json()
    await admin.execute("update public.wallets set status = 'synced' where id = $1", alice_wallet["id"])
    queue.jobs.clear()

    bob_headers = bearer(bob["token"])
    assert (await client.delete(f"/api/wallets/{alice_wallet['id']}", headers=bob_headers)).status_code == 404
    assert (
        await client.post(f"/api/wallets/{alice_wallet['id']}/sync", headers=bob_headers)
    ).status_code == 404
    assert queue.jobs == []
    bob_list = (await client.get("/api/wallets", headers=bob_headers)).json()
    assert alice_wallet["id"] not in [w["id"] for w in bob_list]
    assert (
        await admin.fetchval("select status from public.wallets where id = $1", alice_wallet["id"])
        == "synced"
    )


async def test_resync(client: httpx.AsyncClient, queue: FakeQueue, admin: asyncpg.Connection) -> None:
    session = await wallet_login(client)
    headers = bearer(session["token"])
    wallet = (await client.get("/api/wallets", headers=headers)).json()[0]
    await admin.execute("update public.wallets set status = 'error', sync_error = 'source_unavailable'")
    failed = (await client.get("/api/wallets", headers=headers)).json()[0]
    assert failed["status"] == "error"
    assert "fonte de dados" in failed["error"]

    response = await client.post(f"/api/wallets/{wallet['id']}/sync", headers=headers)
    assert response.json()["status"] == "syncing"
    assert response.json()["error"] is None
    assert queue.jobs[-1][0] == "sync_wallet"
