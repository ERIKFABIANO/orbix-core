from typing import Any

import httpx
import pytest
import respx
from solders.keypair import Keypair

from orbix.attest import memo
from orbix.logging import _scrub, mask_address
from orbix.reports.files import _cell
from orbix.schemas import is_hyperliquid_address, is_solana_address


async def test_health_and_security_headers(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Cache-Control"] == "no-store"
    assert "max-age" in response.headers["Strict-Transport-Security"]
    assert len(response.headers["X-Request-ID"]) == 32


async def test_unknown_host_rejected(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health", headers={"Host": "evil.example"})).status_code == 400


async def test_cors_only_allowed_origin_and_put(client: httpx.AsyncClient) -> None:
    preflight = {
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "authorization,accept-language",
    }
    ok = await client.options(
        "/api/events/x/price", headers={"Origin": "https://app.orbix.test", **preflight}
    )
    assert ok.headers.get("access-control-allow-origin") == "https://app.orbix.test"
    assert "PUT" in ok.headers["access-control-allow-methods"]
    assert "access-control-allow-credentials" not in ok.headers
    bad = await client.options("/api/me", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in bad.headers


async def test_error_format_and_language(client: httpx.AsyncClient) -> None:
    pt = await client.get("/api/me")
    assert pt.status_code == 401
    assert pt.headers["WWW-Authenticate"] == "Bearer"
    body = pt.json()
    assert set(body) == {"error", "request_id"}
    assert body["error"] == {"code": "unauthorized", "message": "Sua sessão expirou. Entre de novo."}

    en = await client.get("/api/me", headers={"Accept-Language": "en-US,en;q=0.9"})
    assert en.json()["error"]["message"] == "Your session expired. Sign in again."

    missing = await client.get("/api/nao-existe")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")


async def test_validation_error_does_not_echo_input(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/auth/email/start", json={"email": "<script>alert(1)</script>"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["fields"] == ["email"]
    assert "script" not in response.text


async def test_unhandled_error_hides_details(client: httpx.AsyncClient) -> None:
    app = client.app  # type: ignore[attr-defined]

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("senha do banco: hunter2")

    response = await client.get("/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal"
    assert "hunter2" not in response.text


async def test_rate_limit_on_login(client: httpx.AsyncClient) -> None:
    address = str(Keypair().pubkey())
    codes = [
        (await client.get("/api/auth/nonce", params={"address": address})).status_code for _ in range(31)
    ]
    assert codes[:30] == [200] * 30
    assert codes[30] == 429


def test_address_validation() -> None:
    assert is_solana_address("11111111111111111111111111111111")
    assert is_solana_address("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
    for bad in ["", "abc", "0OIl" * 8, "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr" + "X" * 10, "<script>"]:
        assert not is_solana_address(bad)
    assert is_hyperliquid_address("0x" + "aF" * 20)
    for bad in ["0x123", "a" * 42, "0x" + "g" * 40]:
        assert not is_hyperliquid_address(bad)


def test_logs_mask_addresses_secrets_and_url_keys() -> None:
    event: dict[str, Any] = {
        "event": "ingest wallet MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr done",
        "hl": "user 0x" + "a" * 40,
        "authorization": "Bearer abc",
        "helius_api_key": "xyz",
        "exception": "GET https://mainnet.helius-rpc.com/v0/addresses/x/transactions?api-key=7fa3e5b8-secret&limit=100 failed",
    }
    out = _scrub(None, "info", event)
    assert "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr" not in out["event"]
    assert "a" * 40 not in out["hl"]
    assert out["authorization"] == "[removido]"
    assert out["helius_api_key"] == "[removido]"
    assert "7fa3e5b8-secret" not in out["exception"]
    assert "api-key=[removido]&limit=100" in out["exception"]
    assert mask_address("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr") == "Memo…fcHr"


def test_csv_cells_neutralize_spreadsheet_formulas() -> None:
    assert _cell('=HYPERLINK("http://evil")') == '"\'=HYPERLINK(""http://evil"")"'
    assert _cell("+SUM(A1)") == "'+SUM(A1)"
    assert _cell("-1+1") == "'-1+1"
    assert _cell("@cmd") == "'@cmd"
    assert _cell("SOL → USDC") == "SOL → USDC"
    assert _cell("A,B") == '"A,B"'


# ── atestação (RPC simulado) ──────────────────────────────────────────────

RPC = "https://rpc.test"


def _rpc_mock(responses: dict[str, Any]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        method = __import__("json").loads(request.content)["method"]
        value = responses[method]
        if isinstance(value, list):
            value = value.pop(0) if len(value) > 1 else value[0]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, **value})

    respx.post(RPC).mock(side_effect=handler)


@respx.mock
async def test_memo_submit_and_confirm(http_out: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(memo.asyncio, "sleep", no_sleep)
    payer = Keypair()
    _rpc_mock(
        {
            "getLatestBlockhash": {
                "result": {"value": {"blockhash": "EkSnNWid2cvwEVnVx9aBqawnmiCNiDgp3gUdkDPTKN1N"}}
            },
            "sendTransaction": {"result": "5hN2memoSignature"},
            "getSignatureStatuses": [
                {"result": {"value": [None]}},
                {"result": {"value": [{"slot": 331508764, "confirmationStatus": "confirmed", "err": None}]}},
            ],
        }
    )
    text = memo.memo_text("a" * 64)
    assert text == "orbix-declare:v1:" + "a" * 64
    signature = await memo.submit_memo(http_out, RPC, payer, text)
    assert signature == "5hN2memoSignature"
    assert await memo.wait_confirmation(http_out, RPC, signature) == 331508764


@respx.mock
async def test_memo_failure_modes(http_out: httpx.AsyncClient) -> None:
    _rpc_mock(
        {
            "getSignatureStatuses": {
                "result": {"value": [{"slot": 1, "confirmationStatus": "confirmed", "err": {"x": 1}}]}
            }
        }
    )
    with pytest.raises(memo.AttestError):
        await memo.confirmed_slot(http_out, RPC, "sig")

    respx.post("https://down.test").mock(side_effect=httpx.ConnectError("x"))
    assert await memo.memo_matches(http_out, "https://down.test", "sig", "t") is None


def test_keypair_formats() -> None:
    original = Keypair()
    assert memo.load_keypair(str(original)).pubkey() == original.pubkey()
    assert memo.load_keypair(str(list(bytes(original)))).pubkey() == original.pubkey()
