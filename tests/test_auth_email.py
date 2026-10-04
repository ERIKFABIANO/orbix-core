import asyncpg
import fakeredis.aioredis
import httpx

from orbix.auth.mailer import render_login_code
from tests.conftest import FakeMailer, bearer, email_login, wallet_login


async def test_register_and_login_with_code(client: httpx.AsyncClient, mailer: FakeMailer) -> None:
    session = await email_login(client, mailer, "Ana@Exemplo.com ")
    user = session["user"]
    assert user["email"] == "ana@exemplo.com"
    assert user["address"] is None
    assert user["loginMethods"] == ["email"]
    assert user["hasWallets"] is False
    assert mailer.sent[0][0] == "ana@exemplo.com"
    assert len(mailer.last_code) == 6 and mailer.last_code.isdigit()

    me = await client.get("/api/me", headers=bearer(session["token"]))
    assert me.json()["id"] == user["id"]


async def test_same_email_is_the_same_account(
    client: httpx.AsyncClient, mailer: FakeMailer, redis: fakeredis.aioredis.FakeRedis
) -> None:
    first = await email_login(client, mailer, "ana@exemplo.com")
    await redis.flushall()  # passa do intervalo de reenvio
    second = await email_login(client, mailer, "ANA@exemplo.com")
    assert first["user"]["id"] == second["user"]["id"]


async def test_start_response_does_not_reveal_if_email_exists(
    client: httpx.AsyncClient, mailer: FakeMailer, redis: fakeredis.aioredis.FakeRedis
) -> None:
    await email_login(client, mailer, "existe@exemplo.com")
    await redis.flushall()
    known = await client.post("/api/auth/email/start", json={"email": "existe@exemplo.com"})
    unknown = await client.post("/api/auth/email/start", json={"email": "novo@exemplo.com"})
    assert known.status_code == unknown.status_code == 200
    assert set(known.json()) == set(unknown.json()) == {"sent", "expiresAt", "resendAfter"}


async def test_wrong_code_then_lockout(client: httpx.AsyncClient, mailer: FakeMailer) -> None:
    await client.post("/api/auth/email/start", json={"email": "ana@exemplo.com"})
    wrong = "000000" if mailer.last_code != "000000" else "111111"
    codes = []
    for _ in range(5):
        response = await client.post(
            "/api/auth/email/verify", json={"email": "ana@exemplo.com", "code": wrong}
        )
        codes.append((response.status_code, response.json()["error"]["code"]))
    assert codes[:4] == [(401, "invalid_code")] * 4
    assert codes[4] == (429, "too_many_attempts")
    # depois do bloqueio, nem o código certo vale mais
    right = await client.post(
        "/api/auth/email/verify", json={"email": "ana@exemplo.com", "code": mailer.last_code}
    )
    assert right.status_code == 401
    assert right.json()["error"]["code"] == "code_expired"


async def test_code_works_only_once(client: httpx.AsyncClient, mailer: FakeMailer) -> None:
    await email_login(client, mailer, "ana@exemplo.com")
    again = await client.post(
        "/api/auth/email/verify", json={"email": "ana@exemplo.com", "code": mailer.last_code}
    )
    assert again.status_code == 401
    assert again.json()["error"]["code"] == "code_expired"


async def test_code_of_one_email_does_not_work_for_another(
    client: httpx.AsyncClient, mailer: FakeMailer
) -> None:
    await client.post("/api/auth/email/start", json={"email": "ana@exemplo.com"})
    response = await client.post(
        "/api/auth/email/verify", json={"email": "bia@exemplo.com", "code": mailer.last_code}
    )
    assert response.status_code == 401


async def test_resend_cooldown(client: httpx.AsyncClient) -> None:
    first = await client.post("/api/auth/email/start", json={"email": "ana@exemplo.com"})
    assert first.json()["resendAfter"] == 60
    second = await client.post("/api/auth/email/start", json={"email": "ana@exemplo.com"})
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"


async def test_redis_never_holds_email_or_code_in_clear(
    client: httpx.AsyncClient, mailer: FakeMailer, redis: fakeredis.aioredis.FakeRedis
) -> None:
    await client.post("/api/auth/email/start", json={"email": "ana@exemplo.com"})
    dump = b""
    for key in await redis.keys("*"):
        dump += key
        kind = await redis.type(key)
        dump += (
            b"".join((await redis.hgetall(key)).values())
            if kind == b"hash"
            else (await redis.get(key) or b"")
        )
    assert b"ana@exemplo.com" not in dump
    assert mailer.last_code.encode() not in dump


async def test_mail_failure_returns_503_and_frees_retry(
    client: httpx.AsyncClient, mailer: FakeMailer
) -> None:
    mailer.fail = True
    failed = await client.post("/api/auth/email/start", json={"email": "ana@exemplo.com"})
    assert failed.status_code == 503
    assert failed.json()["error"]["code"] == "email_unavailable"
    mailer.fail = False
    assert (await client.post("/api/auth/email/start", json={"email": "ana@exemplo.com"})).status_code == 200


async def test_invalid_inputs(client: httpx.AsyncClient) -> None:
    assert (await client.post("/api/auth/email/start", json={"email": "sem-arroba"})).status_code == 422
    bad_code = await client.post("/api/auth/email/verify", json={"email": "a@b.co", "code": "12ab"})
    assert bad_code.status_code == 422
    extra = await client.post("/api/auth/email/start", json={"email": "a@b.co", "plan": "pro"})
    assert extra.status_code == 422


async def test_link_email_to_wallet_account(client: httpx.AsyncClient, mailer: FakeMailer) -> None:
    session = await wallet_login(client)
    linked = await email_login(client, mailer, "ana@exemplo.com", **bearer(session["token"]))
    assert linked["user"]["id"] == session["user"]["id"]
    assert linked["user"]["loginMethods"] == ["email", "wallet"]
    assert linked["user"]["email"] == "ana@exemplo.com"


async def test_cannot_link_email_that_belongs_to_another_account(
    client: httpx.AsyncClient, mailer: FakeMailer, admin: asyncpg.Connection
) -> None:
    await email_login(client, mailer, "ana@exemplo.com")
    other = await wallet_login(client)
    await client.post("/api/auth/email/start", json={"email": "bia@exemplo.com"})  # outro e-mail, ok
    await admin.execute("select 1")
    # tenta vincular o e-mail da Ana à conta da carteira
    from tests.conftest import limiter

    limiter.reset()
    redis = client.app.state.redis  # type: ignore[attr-defined]
    await redis.flushall()
    await client.post(
        "/api/auth/email/start", json={"email": "ana@exemplo.com"}, headers=bearer(other["token"])
    )
    response = await client.post(
        "/api/auth/email/verify",
        json={"email": "ana@exemplo.com", "code": mailer.last_code},
        headers=bearer(other["token"]),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "identity_in_use"


def test_template_contains_code_and_safety_notice() -> None:
    for locale, word in (("pt", "chave privada"), ("en", "private key")):
        subject, html, text = render_login_code("482913", locale)  # type: ignore[arg-type]
        assert "482913" in subject
        assert "482 913" in html and "482 913" in text
        assert word in html and word in text
        assert "<script" not in html.lower()
