from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração lida do ambiente. Segredos em SecretStr nunca aparecem em log ou repr."""

    # local: .env na raiz do repositório. Produção: nenhum arquivo, só variáveis do container.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"

    database_url: SecretStr
    db_ssl: bool = True
    redis_url: SecretStr

    supabase_url: AnyHttpUrl

    helius_api_key: SecretStr
    coingecko_api_key: SecretStr
    anthropic_api_key: SecretStr
    solana_rpc_url: SecretStr
    # só o container do worker define esta variável; a API nunca recebe
    solana_memo_secret_key: SecretStr | None = None

    # Cloudflare R2 (S3). Opcional até o módulo de relatórios existir.
    r2_endpoint: AnyHttpUrl | None = None
    r2_access_key_id: SecretStr | None = None
    r2_secret_access_key: SecretStr | None = None
    r2_bucket: str = "orbix-declare-reports"

    cors_origins: list[str] = []
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]

    @property
    def supabase_base(self) -> str:
        return str(self.supabase_url).rstrip("/")

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
