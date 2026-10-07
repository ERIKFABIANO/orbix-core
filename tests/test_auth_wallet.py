import asyncpg
import httpx
from solders.keypair import Keypair

from orbix.auth.wallet import front_origin
from orbix.config import Settings
from tests.conftest import bearer, wallet_login


def _settings(**overrides: object) -> Settings:
    return Settings(database_url="postgresql://x", redis_url="redis://x", **overrides)  # type: ignore[arg-type]


def test_front_origin_never_comes_back_without_a_scheme() -> None:
    """Sem esquema, a primeira linha da mensagem fica com domínio vazio e a carteira recusa
    como mal formada (relatório de testes de 06/10, B3)."""
    # app_url mal configurado (faltou o https://)
    settings = _settings(app_url="declare.orbixlab.com.br", cors_origins=[])
    assert front_origin(settings, None) == "https://declare.orbixlab.com.br"
    # origem fora da lista de CORS: cai pro app_url, que aqui está correto
    settings = _settings(app_url="https://declare.orbixlab.com.br", cors_origins=["https://app.orbix.test"])
    assert front_origin(settings, "https://evil.example") == "https://declare.orbixlab.com.br"
    # origem liberada: usa ela, não o app_url
    settings = _settings(app_url="https://declare.orbixlab.com.br", cors_origins=["https://app.orbix.test"])
    assert front_origin(settings, "https://app.orbix.test") == "https://app.orbix.test"


async def test_login_creates_account_session_and_login_wallet(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    session = await wallet_login(client)
    address = str(session["keypair"].pubkey())
    user = session["user"]
    assert user["address"] == address
    assert user["loginMethods"] == ["wallet"]
    assert user["hasWallets"] is True
    assert user["plan"] == "free"
    assert user["agentQuestionsLeft"] == 20
    assert user["onboarded"] is False

    me = await client.get("/api/me", headers=bearer(session["token"]))
    assert me.status_code == 200
    assert me.json()["id"] == user["id"]

    wallet = await admin.fetchrow("select * from public.wallets where address = $1", address)
    assert wallet["is_login"] is True
    assert wallet["verified"] is True
    assert wallet["verified_at"] is not None


async def test_token_is_stored_only_as_hash(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    session = await wallet_login(client)
    rows = await admin.fetch("select token_hash from orbix_auth.sessions")
    assert len(rows) == 1
    assert session["token"].encode() not in bytes(rows[0]["token_hash"])
    assert len(rows[0]["token_hash"]) == 32


async def test_second_login_is_the_same_account(client: httpx.AsyncClient) -> None:
    keypair = Keypair()
    first = await wallet_login(client, keypair)
    second = await wallet_login(client, keypair)
    assert first["user"]["id"] == second["user"]["id"]
    assert first["token"] != second["token"]


async def test_message_follows_siws_and_binds_domain(client: httpx.AsyncClient) -> None:
    address = str(Keypair().pubkey())
    body = (await client.get("/api/auth/nonce", params={"address": address})).json()
    lines = body["message"].splitlines()
    assert lines[0] == "app.orbix.test wants you to sign in with your Solana account:"
    assert lines[1] == address
    assert "URI: https://app.orbix.test" in lines
    assert f"Nonce: {body['nonce']}" in lines
    assert any(line.startswith("Expiration Time: ") for line in lines)


async def test_message_domain_follows_allowed_origin_only(client: httpx.AsyncClient) -> None:
    address = str(Keypair().pubkey())
    allowed = (
        await client.get(
            "/api/auth/nonce", params={"address": address}, headers={"Origin": "https://app.orbix.test"}
        )
    ).json()["message"]
    assert allowed.startswith("app.orbix.test wants you to sign in")
    assert "Chain ID: mainnet" in allowed
    # origem fora da lista do CORS nunca entra na mensagem: vale o endereço oficial do front
    evil = (
        await client.get(
            "/api/auth/nonce", params={"address": address}, headers={"Origin": "https://evil.example"}
        )
    ).json()["message"]
    assert evil.startswith("app.orbix.test wants you to sign in")
    assert "evil" not in evil


async def test_statement_follows_accept_language(client: httpx.AsyncClient) -> None:
    address = str(Keypair().pubkey())
    pt = (await client.get("/api/auth/nonce", params={"address": address})).json()["message"]
    en = (
        await client.get("/api/auth/nonce", params={"address": address}, headers={"Accept-Language": "en-US"})
    ).json()["message"]
    assert "Entrar no Orbix Declare" in pt
    assert "Sign in to Orbix Declare" in en


async def test_nonce_cannot_be_replayed(client: httpx.AsyncClient) -> None:
    keypair = Keypair()
    address = str(keypair.pubkey())
    nonce = (await client.get("/api/auth/nonce", params={"address": address})).json()
    body = {
        "address": address,
        "message": nonce["message"],
        "signature": str(keypair.sign_message(nonce["message"].encode())),
    }
    assert (await client.post("/api/auth/verify", json=body)).status_code == 200
    replay = await client.post("/api/auth/verify", json=body)
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "nonce_expired"


async def test_signature_from_another_key_is_rejected(client: httpx.AsyncClient) -> None:
    victim, attacker = Keypair(), Keypair()
    address = str(victim.pubkey())
    nonce = (await client.get("/api/auth/nonce", params={"address": address})).json()
    response = await client.post(
        "/api/auth/verify",
        json={
            "address": address,
            "message": nonce["message"],
            "signature": str(attacker.sign_message(nonce["message"].encode())),
        },
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"


async def test_nonce_issued_to_one_address_cannot_log_in_another(client: httpx.AsyncClient) -> None:
    victim, attacker = Keypair(), Keypair()
    nonce = (await client.get("/api/auth/nonce", params={"address": str(victim.pubkey())})).json()
    # o atacante assina, com a própria chave, a mensagem emitida para a vítima
    response = await client.post(
        "/api/auth/verify",
        json={
            "address": str(attacker.pubkey()),
            "message": nonce["message"],
            "signature": str(attacker.sign_message(nonce["message"].encode())),
        },
    )
    assert response.status_code == 401


async def test_tampered_message_is_rejected(client: httpx.AsyncClient) -> None:
    keypair = Keypair()
    address = str(keypair.pubkey())
    nonce = (await client.get("/api/auth/nonce", params={"address": address})).json()
    tampered = nonce["message"].replace("app.orbix.test", "evil.example")
    response = await client.post(
        "/api/auth/verify",
        json={
            "address": address,
            "message": tampered,
            "signature": str(keypair.sign_message(tampered.encode())),
        },
    )
    assert response.status_code == 401


async def test_invalid_address(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/auth/nonce", params={"address": "nao-e-endereco"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_address"


async def test_logout_revokes_the_session(client: httpx.AsyncClient) -> None:
    session = await wallet_login(client)
    headers = bearer(session["token"])
    assert (await client.post("/api/auth/logout", headers=headers)).status_code == 204
    after = await client.get("/api/me", headers=headers)
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "unauthorized"


async def test_expired_session_is_rejected(client: httpx.AsyncClient, admin: asyncpg.Connection) -> None:
    session = await wallet_login(client)
    await admin.execute("update orbix_auth.sessions set expires_at = now() - interval '1 second'")
    assert (await client.get("/api/me", headers=bearer(session["token"]))).status_code == 401


async def test_protected_routes_require_token(client: httpx.AsyncClient) -> None:
    for method, path in [
        ("GET", "/api/me"),
        ("GET", "/api/wallets"),
        ("POST", "/api/ingest"),
        ("GET", "/api/ingest/status"),
        ("GET", "/api/dashboard"),
        ("GET", "/api/events?month=2026-09"),
        ("GET", "/api/reports"),
        ("GET", "/api/report/2026-09"),
        ("POST", "/api/agent"),
        ("POST", "/api/auth/logout"),
        ("DELETE", "/api/me"),
    ]:
        response = await client.request(method, path, json={} if method == "POST" else None)
        assert response.status_code == 401, (method, path, response.status_code)
        assert response.json()["error"]["code"] == "unauthorized"
        assert response.json()["error"]["message"]


async def test_garbage_tokens(client: httpx.AsyncClient) -> None:
    for token in ["x", "a" * 43, "../../etc/passwd", "' or 1=1 --"]:
        assert (await client.get("/api/me", headers=bearer(token))).status_code == 401


async def test_delete_account_removes_everything(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    session = await wallet_login(client)
    assert (await client.delete("/api/me", headers=bearer(session["token"]))).status_code == 204
    for table in ("public.profiles", "public.wallets", "orbix_auth.identities", "orbix_auth.sessions"):
        assert await admin.fetchval(f"select count(*) from {table}") == 0
    assert (await client.get("/api/me", headers=bearer(session["token"]))).status_code == 401
