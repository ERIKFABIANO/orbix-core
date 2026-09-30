import base64
import json
import time
import uuid
from collections.abc import Callable

import jwt
from fastapi.testclient import TestClient

from tests.conftest import ISSUER


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_me_without_token(client: TestClient) -> None:
    response = client.get("/me")
    assert response.status_code == 401
    assert "request_id" in response.json()


def test_me_with_valid_token(client: TestClient, make_token: Callable[..., str]) -> None:
    user_id = str(uuid.uuid4())
    response = client.get("/me", headers=_bearer(make_token(sub=user_id)))
    assert response.status_code == 200
    assert response.json() == {"id": user_id}


def test_garbage_token(client: TestClient) -> None:
    assert client.get("/me", headers=_bearer("nao.e.jwt")).status_code == 401


def test_expired_token(client: TestClient, make_token: Callable[..., str]) -> None:
    past = int(time.time()) - 7200
    token = make_token(iat=past, exp=past + 60)
    assert client.get("/me", headers=_bearer(token)).status_code == 401


def test_wrong_audience(client: TestClient, make_token: Callable[..., str]) -> None:
    assert client.get("/me", headers=_bearer(make_token(aud="anon"))).status_code == 401


def test_wrong_issuer(client: TestClient, make_token: Callable[..., str]) -> None:
    token = make_token(iss="https://evil.supabase.co/auth/v1")
    assert client.get("/me", headers=_bearer(token)).status_code == 401


def test_sub_not_uuid(client: TestClient, make_token: Callable[..., str]) -> None:
    assert client.get("/me", headers=_bearer(make_token(sub="admin"))).status_code == 401


def test_alg_none_rejected(client: TestClient) -> None:
    now = int(time.time())
    claims = {"sub": str(uuid.uuid4()), "aud": "authenticated", "iss": ISSUER,
              "iat": now, "exp": now + 3600}

    def b64(data: dict[str, object]) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."
    assert client.get("/me", headers=_bearer(token)).status_code == 401


def test_hs256_confusion_rejected(client: TestClient) -> None:
    now = int(time.time())
    claims = {"sub": str(uuid.uuid4()), "aud": "authenticated", "iss": ISSUER,
              "iat": now, "exp": now + 3600}
    token = jwt.encode(claims, "segredo-qualquer-com-32-bytes-ok!", algorithm="HS256")
    assert client.get("/me", headers=_bearer(token)).status_code == 401


def test_rate_limit_per_user(client: TestClient, make_token: Callable[..., str]) -> None:
    token = make_token()
    codes = [client.get("/me", headers=_bearer(token)).status_code for _ in range(31)]
    assert codes[:30] == [200] * 30
    assert codes[30] == 429
