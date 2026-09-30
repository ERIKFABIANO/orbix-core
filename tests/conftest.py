import os
import time
import uuid
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

# ambiente de teste antes de importar a aplicação
os.environ.update(
    {
        "ENV": "test",
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
        "DB_SSL": "false",
        "REDIS_URL": "redis://localhost:6379/0",
        "SUPABASE_URL": "https://test.supabase.co",
        "HELIUS_API_KEY": "test",
        "COINGECKO_API_KEY": "test",
        "ANTHROPIC_API_KEY": "test",
        "SOLANA_RPC_URL": "https://api.devnet.solana.com",
        "CORS_ORIGINS": '["https://app.orbix.test"]',
        "ALLOWED_HOSTS": '["testserver"]',
    }
)

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from orbix import auth
from orbix.main import create_app
from orbix.ratelimit import limiter

ISSUER = "https://test.supabase.co/auth/v1"


@pytest.fixture(scope="session")
def signing_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture(autouse=True)
def fake_jwks(
    monkeypatch: pytest.MonkeyPatch, signing_key: ec.EllipticCurvePrivateKey
) -> None:
    public = signing_key.public_key()
    client = SimpleNamespace(get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=public))
    monkeypatch.setattr(auth, "jwks_client", lambda: client)


@pytest.fixture(autouse=True)
def reset_limiter() -> Iterator[None]:
    limiter.reset()
    yield


@pytest.fixture
def make_token(signing_key: ec.EllipticCurvePrivateKey) -> Callable[..., str]:
    def _make(**overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": str(uuid.uuid4()),
            "aud": "authenticated",
            "iss": ISSUER,
            "iat": now,
            "exp": now + 3600,
            "role": "authenticated",
        }
        claims.update(overrides)
        return jwt.encode(claims, signing_key, algorithm="ES256")

    return _make


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)
