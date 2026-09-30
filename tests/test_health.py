from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_security_headers(client: TestClient) -> None:
    response = client.get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "max-age" in response.headers["Strict-Transport-Security"]
    assert len(response.headers["X-Request-ID"]) == 32


def test_unknown_host_rejected(client: TestClient) -> None:
    response = client.get("/health", headers={"Host": "evil.example"})
    assert response.status_code == 400


def test_cors_only_allowed_origin(client: TestClient) -> None:
    preflight = {"Access-Control-Request-Method": "GET"}
    ok = client.options("/me", headers={"Origin": "https://app.orbix.test", **preflight})
    assert ok.headers.get("access-control-allow-origin") == "https://app.orbix.test"

    bad = client.options("/me", headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in bad.headers
