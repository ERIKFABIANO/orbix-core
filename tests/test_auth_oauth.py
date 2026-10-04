from urllib.parse import parse_qs, urlsplit

import fakeredis.aioredis
import httpx
import respx

from tests.conftest import FakeMailer, bearer, email_login, wallet_login


def _fragment(location: str) -> dict[str, str]:
    assert location.startswith("https://app.orbix.test/auth/callback#")
    return {k: v[0] for k, v in parse_qs(location.split("#", 1)[1]).items()}


async def _start(client: httpx.AsyncClient, provider: str, **kwargs: object) -> dict[str, str]:
    response = await client.get(f"/api/auth/oauth/{provider}/start", **kwargs)  # type: ignore[arg-type]
    assert response.status_code == 200, response.text
    url = response.json()["url"]
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()} | {"_url": url}


def _mock_google(email: str = "ana@exemplo.com", verified: bool = True, sub: str = "g-1") -> None:
    respx.post("https://oauth2.googleapis.com/token").respond(json={"access_token": "at"})
    respx.get("https://openidconnect.googleapis.com/v1/userinfo").respond(
        json={"sub": sub, "email": email, "email_verified": verified, "name": "Ana Souza"}
    )


def _mock_github(email: str = "ana@exemplo.com", verified: bool = True) -> None:
    respx.post("https://github.com/login/oauth/access_token").respond(json={"access_token": "at"})
    respx.get("https://api.github.com/user").respond(json={"id": 42, "login": "ana", "name": None})
    respx.get("https://api.github.com/user/emails").respond(
        json=[
            {"email": "outro@exemplo.com", "primary": False, "verified": True},
            {"email": email, "primary": True, "verified": verified},
        ]
    )


async def test_start_builds_authorize_url_with_state_and_pkce(client: httpx.AsyncClient) -> None:
    params = await _start(client, "google", params={"next": "/painel"})
    assert params["_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert params["client_id"] == "google-id"
    assert params["redirect_uri"] == "https://api.orbix.test/api/auth/oauth/google/callback"
    assert params["code_challenge_method"] == "S256"
    assert len(params["state"]) >= 40 and len(params["code_challenge"]) >= 40
    assert "client_secret" not in params["_url"]


@respx.mock
async def test_google_login_end_to_end(client: httpx.AsyncClient) -> None:
    _mock_google()
    params = await _start(client, "google", params={"next": "/relatorios"})
    callback = await client.get(
        "/api/auth/oauth/google/callback", params={"code": "abc", "state": params["state"]}
    )
    assert callback.status_code == 302
    assert callback.headers["cache-control"] == "no-store"
    fragment = _fragment(callback.headers["location"])
    assert fragment["next"] == "/relatorios"
    # o token de sessão nunca aparece na URL
    assert "token" not in callback.headers["location"].lower()

    session = (await client.post("/api/auth/exchange", json={"code": fragment["code"]})).json()
    assert session["user"]["email"] == "ana@exemplo.com"
    assert session["user"]["displayName"] == "Ana Souza"
    assert session["user"]["loginMethods"] == ["google"]
    assert (await client.get("/api/me", headers=bearer(session["token"]))).status_code == 200

    # o código de troca só funciona uma vez
    again = await client.post("/api/auth/exchange", json={"code": fragment["code"]})
    assert again.status_code == 401
    assert again.json()["error"]["code"] == "code_expired"


@respx.mock
async def test_github_uses_primary_verified_email(client: httpx.AsyncClient) -> None:
    _mock_github()
    params = await _start(client, "github")
    callback = await client.get(
        "/api/auth/oauth/github/callback", params={"code": "abc", "state": params["state"]}
    )
    code = _fragment(callback.headers["location"])["code"]
    session = (await client.post("/api/auth/exchange", json={"code": code})).json()
    assert session["user"]["email"] == "ana@exemplo.com"
    assert session["user"]["displayName"] == "ana"
    assert session["user"]["loginMethods"] == ["github"]


@respx.mock
async def test_same_verified_email_merges_accounts(
    client: httpx.AsyncClient, mailer: FakeMailer, redis: fakeredis.aioredis.FakeRedis
) -> None:
    by_email = await email_login(client, mailer, "ana@exemplo.com")
    _mock_google()
    params = await _start(client, "google")
    callback = await client.get(
        "/api/auth/oauth/google/callback", params={"code": "abc", "state": params["state"]}
    )
    code = _fragment(callback.headers["location"])["code"]
    session = (await client.post("/api/auth/exchange", json={"code": code})).json()
    assert session["user"]["id"] == by_email["user"]["id"]
    assert session["user"]["loginMethods"] == ["email", "google"]


@respx.mock
async def test_unverified_email_is_refused(client: httpx.AsyncClient) -> None:
    _mock_google(verified=False)
    params = await _start(client, "google")
    callback = await client.get(
        "/api/auth/oauth/google/callback", params={"code": "abc", "state": params["state"]}
    )
    assert _fragment(callback.headers["location"]) == {"error": "email_not_verified"}


@respx.mock
async def test_state_is_single_use_and_bound_to_provider(client: httpx.AsyncClient) -> None:
    _mock_google()
    _mock_github()
    params = await _start(client, "google")
    # state emitido para o Google não serve no callback do GitHub
    wrong = await client.get(
        "/api/auth/oauth/github/callback", params={"code": "abc", "state": params["state"]}
    )
    assert _fragment(wrong.headers["location"]) == {"error": "oauth_state"}
    # e, depois de lido, não serve mais nem no provedor certo
    reused = await client.get(
        "/api/auth/oauth/google/callback", params={"code": "abc", "state": params["state"]}
    )
    assert _fragment(reused.headers["location"]) == {"error": "oauth_state"}


async def test_forged_or_missing_state(client: httpx.AsyncClient) -> None:
    for params in ({"code": "abc", "state": "forjado"}, {"code": "abc"}, {}):
        callback = await client.get("/api/auth/oauth/google/callback", params=params)
        assert callback.status_code == 302
        assert _fragment(callback.headers["location"]) == {"error": "oauth_state"}


@respx.mock
async def test_provider_error_and_token_failure(client: httpx.AsyncClient) -> None:
    params = await _start(client, "google")
    denied = await client.get(
        "/api/auth/oauth/google/callback", params={"error": "access_denied", "state": params["state"]}
    )
    assert _fragment(denied.headers["location"]) == {"error": "oauth_failed"}

    respx.post("https://oauth2.googleapis.com/token").respond(400, json={"error": "invalid_grant"})
    params = await _start(client, "google")
    failed = await client.get(
        "/api/auth/oauth/google/callback", params={"code": "abc", "state": params["state"]}
    )
    assert _fragment(failed.headers["location"]) == {"error": "oauth_failed"}


@respx.mock
async def test_next_cannot_redirect_to_another_site(client: httpx.AsyncClient) -> None:
    _mock_google()
    for evil in ("https://evil.example", "//evil.example", "/\\evil.example", "javascript:alert(1)"):
        params = await _start(client, "google", params={"next": evil})
        callback = await client.get(
            "/api/auth/oauth/google/callback", params={"code": "abc", "state": params["state"]}
        )
        location = callback.headers["location"]
        assert location.startswith("https://app.orbix.test/auth/callback#")
        assert "next" not in _fragment(location)


@respx.mock
async def test_link_google_to_wallet_account(client: httpx.AsyncClient) -> None:
    wallet = await wallet_login(client)
    _mock_google()
    params = await _start(client, "google", headers=bearer(wallet["token"]))
    callback = await client.get(
        "/api/auth/oauth/google/callback", params={"code": "abc", "state": params["state"]}
    )
    code = _fragment(callback.headers["location"])["code"]
    session = (await client.post("/api/auth/exchange", json={"code": code})).json()
    assert session["user"]["id"] == wallet["user"]["id"]
    assert session["user"]["loginMethods"] == ["google", "wallet"]


async def test_unknown_provider_and_providers_list(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/auth/oauth/facebook/start")).status_code == 404
    providers = (await client.get("/api/auth/providers")).json()
    assert providers == {"wallet": True, "email": True, "google": True, "github": True}
