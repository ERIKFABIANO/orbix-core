import os
from decimal import Decimal
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração lida do ambiente. Segredos em SecretStr nunca aparecem em log ou repr."""

    # local: .env na raiz do repositório. Produção: nenhum arquivo, só variáveis do container.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"

    # ── banco e fila ──
    database_url: SecretStr
    # só para rodar o worker localmente; em produção o worker recebe DATABASE_URL próprio
    worker_database_url: SecretStr | None = None
    db_ssl: bool = True
    redis_url: SecretStr

    # ── endereços públicos ──
    app_url: str = "http://localhost:3000"  # front
    api_url: str = "http://localhost:8000"  # esta API, como o navegador a enxerga
    cors_origins: list[str] = []
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]

    # ── sessão e códigos ──
    # chave do HMAC dos códigos de e-mail; obrigatória em produção
    secret_key: SecretStr | None = None
    session_ttl_hours: int = 168

    # ── login por e-mail e social (desligados enquanto a chave não existir) ──
    resend_api_key: SecretStr | None = None
    email_from: str = "Orbix Declare <no-reply@orbixlab.com.br>"
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    github_client_id: str | None = None
    github_client_secret: SecretStr | None = None

    # ── dados on-chain e preços ──
    helius_api_key: SecretStr | None = None
    coingecko_api_key: SecretStr | None = None
    ingest_max_transactions: int = 10000

    # ── agente ──
    openai_api_key: SecretStr | None = None
    llm_model_agent: str = "gpt-5.4"
    llm_model_classify: str = "gpt-5.4-mini"

    # ── atestação na Solana ──
    solana_memo_rpc_url: SecretStr = SecretStr("https://api.devnet.solana.com")
    solana_memo_cluster: Literal["devnet", "mainnet-beta", "testnet"] = "devnet"
    # só o container do worker define esta variável; a API nunca recebe
    solana_memo_secret_key: SecretStr | None = None

    # ── Cloudflare R2 ──
    r2_endpoint: str | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_bucket: str = "orbixdeclare"

    # ── regras de produto ──
    exemption_limit_brl: Decimal = Decimal("35000")
    capital_gain_rate: Decimal = Decimal("0.15")
    # custo do que foi vendido sem aquisição conhecida: "zero" (regra da Receita) ou "market"
    unknown_cost_policy: Literal["zero", "market"] = "zero"
    free_wallet_limit: int = 3
    free_agent_questions: int = 20
    paid_agent_questions: int = 500

    @field_validator("*", mode="before")
    @classmethod
    def _empty_is_missing(cls, value: Any) -> Any:
        # `CHAVE=` no .env significa "não configurado", não string vazia
        return None if isinstance(value, str) and value.strip() == "" else value

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def app_domain(self) -> str:
        return urlsplit(self.app_url).netloc

    @property
    def signing_key(self) -> bytes:
        if self.secret_key is not None:
            return self.secret_key.get_secret_value().encode()
        if self.is_prod:
            raise RuntimeError("SECRET_KEY é obrigatória em produção")
        return b"orbix-dev-only-key"

    @property
    def providers(self) -> dict[str, bool]:
        return {
            "wallet": True,
            "email": self.resend_api_key is not None or not self.is_prod,
            "google": bool(self.google_client_id and self.google_client_secret),
            "github": bool(self.github_client_id and self.github_client_secret),
        }

    @property
    def storage_enabled(self) -> bool:
        return bool(self.r2_endpoint and self.r2_access_key_id and self.r2_secret_access_key)


@lru_cache
def get_settings() -> Settings:
    # nos testes, o .env local (com chaves de verdade) nunca é lido
    if os.environ.get("ENV") == "test":
        return Settings(_env_file=None)
    return Settings()
