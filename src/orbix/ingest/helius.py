"""Cliente da Helius: histórico decodificado e metadados de tokens."""

import asyncio
from typing import Any

import httpx
import structlog

from orbix.ingest.models import IngestError

log = structlog.get_logger()

BASE = "https://mainnet.helius-rpc.com"
PAGE_LIMIT = 100
MAX_TRIES = 4


def ipv4_client() -> httpx.AsyncClient:
    """A chave da Helius é liberada por IPv4. Sem isto, o sistema pode sair por IPv6 e levar 403."""
    return httpx.AsyncClient(
        transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0", retries=1),  # noqa: S104  # nosec B104
        timeout=30,
    )


class HeliusClient:
    def __init__(self, api_key: str, http: httpx.AsyncClient) -> None:
        self._key = api_key
        self._http = http

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        # a chave vai na query string: nunca logar a URL nem a exceção crua do httpx
        params = {**kwargs.pop("params", {}), "api-key": self._key}
        for attempt in range(MAX_TRIES):
            try:
                response = await self._http.request(method, url, params=params, **kwargs)
            except httpx.HTTPError as exc:
                log.warning("helius: falha de rede", kind=type(exc).__name__, attempt=attempt)
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if response.status_code == 200:
                return response.json()
            if response.status_code in (401, 403):
                log.error("helius: chave recusada", status=response.status_code)
                raise IngestError("source_rejected")
            if response.status_code == 429 or response.status_code >= 500:
                log.warning("helius: indisponível", status=response.status_code, attempt=attempt)
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            raise IngestError("source_rejected")
        raise IngestError("source_unavailable")

    async def transactions(self, address: str, before: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": PAGE_LIMIT}
        if before:
            params["before"] = before
        data = await self._request("GET", f"{BASE}/v0/addresses/{address}/transactions", params=params)
        return data if isinstance(data, list) else []

    async def token_metadata(self, mints: list[str]) -> dict[str, tuple[str | None, int | None]]:
        """mint -> (símbolo, casas decimais). Símbolo é dado on-chain: só para exibir, limitado."""
        found: dict[str, tuple[str | None, int | None]] = {}
        for start in range(0, len(mints), 100):
            chunk = mints[start : start + 100]
            data = await self._request(
                "POST",
                f"{BASE}/",
                json={"jsonrpc": "2.0", "id": "orbix", "method": "getAssetBatch", "params": {"ids": chunk}},
            )
            for item in (data or {}).get("result") or []:
                if not isinstance(item, dict):
                    continue
                info = item.get("token_info") or {}
                metadata = (item.get("content") or {}).get("metadata") or {}
                symbol = info.get("symbol") or metadata.get("symbol")
                decimals = info.get("decimals")
                found[str(item.get("id"))] = (
                    _clean_symbol(symbol),
                    decimals if isinstance(decimals, int) else None,
                )
        return found


def _clean_symbol(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    # símbolo vem de quem criou o token: tirar tudo que não é letra, número ou ponto
    cleaned = "".join(c for c in value if c.isalnum() or c in ".-_$")[:16]
    return cleaned or None
