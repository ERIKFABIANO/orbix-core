"""Base dos testes.

Os testes de integração usam um Postgres local igual ao schema de produção
(`uv run python scripts/testdb.py up`). Sem ele, esses testes são pulados.
Redis, fila, e-mail, storage e modelo de linguagem são falsos, em memória.
"""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

TEST_DB = "127.0.0.1:54329/postgres"

# ambiente de teste antes de importar a aplicação
os.environ.update(
    {
        "ENV": "test",
        "DATABASE_URL": f"postgresql://orbix_api:test@{TEST_DB}",
        "WORKER_DATABASE_URL": f"postgresql://orbix_worker:test@{TEST_DB}",
        "DB_SSL": "false",
        "REDIS_URL": "redis://localhost:6379/0",
        "SECRET_KEY": "chave-de-teste-com-mais-de-32-caracteres",
        "APP_URL": "https://app.orbix.test",
        "API_URL": "https://api.orbix.test",
        "CORS_ORIGINS": '["https://app.orbix.test"]',
        "ALLOWED_HOSTS": '["testserver"]',
        "GOOGLE_CLIENT_ID": "google-id",
        "GOOGLE_CLIENT_SECRET": "google-secret",
        "GITHUB_CLIENT_ID": "github-id",
        "GITHUB_CLIENT_SECRET": "github-secret",
        "RESEND_API_KEY": "",
        "OPENAI_API_KEY": "",
        "HELIUS_API_KEY": "",
        "COINGECKO_API_KEY": "",
        "R2_ENDPOINT": "",
        "SOLANA_MEMO_SECRET_KEY": "",
    }
)

import asyncpg
import fakeredis.aioredis
import httpx
import pytest
from solders.keypair import Keypair

from orbix.agent.llm import AgentAnswer, BreakdownLine
from orbix.config import get_settings
from orbix.db import Database
from orbix.main import create_app
from orbix.ratelimit import limiter

ADMIN_DSN = f"postgresql://postgres:test@{TEST_DB}"
TABLES = (
    "auth.users, public.agent_messages, public.event_reviews, public.ingestion_runs, "
    "public.report_versions, public.events, public.reports, public.wallets, public.profiles, "
    "public.prices, public.fx_rates, public.assets, orbix_auth.identities, orbix_auth.sessions"
)


class FakeQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple[Any, ...], str | None]] = []

    async def enqueue(self, job: str, *args: Any, job_id: str | None = None) -> None:
        self.jobs.append((job, args, job_id))


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.fail = False

    async def send_login_code(self, to: str, code: str, locale: str) -> None:
        if self.fail:
            from orbix.errors import AppError

            raise AppError("email_unavailable", 503)
        self.sent.append((to, code, locale))

    @property
    def last_code(self) -> str:
        return self.sent[-1][1]


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = data

    async def presign(self, key: str, filename: str) -> tuple[str, datetime]:
        return f"https://r2.test/{key}?sig=x", datetime.now(UTC) + timedelta(minutes=10)

    async def delete_prefix(self, prefix: str) -> None:
        for key in [k for k in self.objects if k.startswith(prefix)]:
            del self.objects[key]


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, str]]]] = []
        self.answer_value = AgentAnswer(
            text="O ganho veio da venda de SOL.",
            breakdown=[BreakdownLine(label="Ganho de capital", value="R$ 100,00", emphasis="gain")],
            cited_rows=["r1", "r999"],
            suggestions=["Como foi calculado o custo médio?"],
        )

    async def answer(self, system: str, messages: list[dict[str, str]]) -> AgentAnswer:
        self.calls.append((system, messages))
        return self.answer_value


@pytest.fixture(autouse=True)
def _reset_limiter() -> None:
    limiter.reset()


@pytest.fixture
async def admin() -> AsyncIterator[asyncpg.Connection]:
    """Conexão de administrador no banco de teste. Limpa as tabelas antes de cada teste."""
    try:
        conn = await asyncpg.connect(ADMIN_DSN, timeout=3)
    except (OSError, asyncpg.PostgresError):
        pytest.skip("banco de teste fora do ar: rode `uv run python scripts/testdb.py up`")
    await conn.set_type_codec(
        "jsonb", encoder=__import__("json").dumps, decoder=__import__("json").loads, schema="pg_catalog"
    )
    await conn.execute(f"truncate {TABLES} cascade")
    yield conn
    await conn.close()


@pytest.fixture
async def db(admin: asyncpg.Connection) -> AsyncIterator[Database]:
    database = Database(get_settings().database_url.get_secret_value(), ssl=False, max_size=4)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def worker_db(admin: asyncpg.Connection) -> AsyncIterator[Database]:
    settings = get_settings()
    assert settings.worker_database_url is not None
    database = Database(settings.worker_database_url.get_secret_value(), ssl=False, max_size=2)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
def redis() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
async def http_out() -> AsyncIterator[httpx.AsyncClient]:
    """Cliente HTTP que a aplicação usa para falar com serviços externos (interceptado pelo respx)."""
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
async def client(
    db: Database,
    redis: fakeredis.aioredis.FakeRedis,
    queue: FakeQueue,
    mailer: FakeMailer,
    storage: FakeStorage,
    llm: FakeLLM,
    http_out: httpx.AsyncClient,
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        {
            "db": db,
            "redis": redis,
            "queue": queue,
            "mailer": mailer,
            "storage": storage,
            "llm": llm,
            "http": http_out,
        }
    )
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test_client:
        test_client.app = app  # type: ignore[attr-defined]
        yield test_client


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def wallet_login(
    client: httpx.AsyncClient, keypair: Keypair | None = None, **headers: str
) -> dict[str, Any]:
    """Faz o login com carteira de ponta a ponta e devolve a sessão."""
    keypair = keypair or Keypair()
    address = str(keypair.pubkey())
    nonce = (await client.get("/api/auth/nonce", params={"address": address})).json()
    signature = str(keypair.sign_message(nonce["message"].encode()))
    response = await client.post(
        "/api/auth/verify",
        json={"address": address, "message": nonce["message"], "signature": signature},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return {**response.json(), "keypair": keypair}


async def email_login(
    client: httpx.AsyncClient, mailer: FakeMailer, email: str, **headers: str
) -> dict[str, Any]:
    started = await client.post("/api/auth/email/start", json={"email": email}, headers=headers)
    assert started.status_code == 200, started.text
    response = await client.post(
        "/api/auth/email/verify", json={"email": email, "code": mailer.last_code}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()
