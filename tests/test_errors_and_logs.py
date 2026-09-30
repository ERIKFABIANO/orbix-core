from typing import Any

from fastapi.testclient import TestClient

from orbix.logging import _scrub, mask_address
from orbix.main import create_app


def test_unhandled_error_hides_details() -> None:
    app = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("senha do banco: hunter2")

    response = TestClient(app, raise_server_exceptions=False).get("/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "erro interno"
    assert "hunter2" not in response.text
    assert body["request_id"]


def test_mask_address() -> None:
    assert mask_address("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr") == "Memo…fcHr"


def test_scrub_masks_addresses_and_secrets() -> None:
    event: dict[str, Any] = {
        "event": "ingest wallet MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr done",
        "hl": "user 0x" + "a" * 40,
        "authorization": "Bearer abc",
        "helius_api_key": "xyz",
    }
    out = _scrub(None, "info", event)
    assert "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr" not in out["event"]
    assert "a" * 40 not in out["hl"]
    assert out["authorization"] == "[removido]"
    assert out["helius_api_key"] == "[removido]"
