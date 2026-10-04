"""Preço histórico em dólar pela CoinGecko. Funciona sem chave, com limite bem menor."""

from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

BASE = "https://api.coingecko.com/api/v3"


class RateLimited(Exception):
    """A CoinGecko pediu para esperar. Os eventos ficam sem preço e entram na próxima rodada."""


class NotListed(Exception):
    pass


class CoinGeckoClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str | None, budget: int = 30) -> None:
        self._http = http
        self._headers = {"x-cg-demo-api-key": api_key} if api_key else {}
        # teto de chamadas por rodada: a cota mensal do plano Demo é pequena
        self.budget = budget

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self.budget <= 0:
            raise RateLimited
        self.budget -= 1
        try:
            response = await self._http.get(f"{BASE}{path}", params=params, headers=self._headers, timeout=20)
        except httpx.HTTPError:
            raise RateLimited from None
        if response.status_code == 404:
            raise NotListed
        if response.status_code == 429 or response.status_code >= 500:
            raise RateLimited
        if response.status_code >= 400:
            # 401/403: fora do histórico permitido pelo plano
            raise NotListed
        return response.json()

    async def resolve_solana_mint(self, mint: str) -> tuple[str, str | None]:
        """mint -> (id na CoinGecko, símbolo)."""
        data = await self._get(f"/coins/solana/contract/{mint}")
        coin_id = data.get("id")
        if not isinstance(coin_id, str) or not coin_id:
            raise NotListed
        symbol = data.get("symbol")
        return coin_id, symbol.upper()[:16] if isinstance(symbol, str) else None

    async def price_range(
        self, coin_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, Decimal]]:
        data = await self._get(
            f"/coins/{coin_id}/market_chart/range",
            {"vs_currency": "usd", "from": int(start.timestamp()), "to": int(end.timestamp())},
        )
        points: list[tuple[datetime, Decimal]] = []
        for item in data.get("prices") or []:
            if isinstance(item, list) and len(item) == 2 and item[1] is not None:
                points.append(
                    (datetime.fromtimestamp(item[0] / 1000, tz=start.tzinfo), Decimal(str(item[1])))
                )
        return points
