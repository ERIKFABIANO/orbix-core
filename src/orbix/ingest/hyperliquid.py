"""Fills e funding da Hyperliquid. A API de informação é pública, sem chave."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from orbix.ingest.models import EventDraft, IngestError

INFO_URL = "https://api.hyperliquid.xyz/info"
USDC = "USDC"
PAGE_LIMIT = 2000


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _is_spot(coin: str) -> bool:
    return coin.startswith("@") or "/" in coin


def normalize_fill(fill: dict[str, Any]) -> list[EventDraft]:
    coin = str(fill.get("coin") or "")[:40]
    price, size = _decimal(fill.get("px")), _decimal(fill.get("sz"))
    time_ms, tid = fill.get("time"), fill.get("tid")
    if not coin or price is None or size is None or size <= 0 or not isinstance(time_ms, int):
        return []
    ts = datetime.fromtimestamp(time_ms / 1000, UTC)
    tx_hash = str(fill.get("hash") or f"fill-{tid}")[:100]
    # vários fills podem dividir o mesmo hash; o id do fill torna o índice único e estável
    base_index = (int(tid) % 1_000_000_000) * 2 if isinstance(tid, int) else 0
    side = str(fill.get("side") or "")
    raw = {
        "side": side,
        "dir": str(fill.get("dir") or "")[:30],
        "px": str(price),
        "closedPnl": str(_decimal(fill.get("closedPnl")) or Decimal(0)),
        "fee": str(_decimal(fill.get("fee")) or Decimal(0)),
        "feeToken": str(fill.get("feeToken") or USDC)[:20],
    }

    if not _is_spot(coin):
        return [
            EventDraft(
                tx_hash=tx_hash,
                event_index=base_index,
                ts=ts,
                kind="perp_fill",
                asset=coin,
                qty=size,
                raw=raw,
                usd_price=price,
                pricing_policy="hyperliquid_fill",
            )
        ]

    # spot: troca entre o ativo e USDC. "B" compra o ativo; "A" vende
    quote = price * size
    base = (coin, size, price)
    usdc = (USDC, quote, Decimal(1))
    out, into = (usdc, base) if side == "B" else (base, usdc)
    return [
        EventDraft(
            tx_hash=tx_hash,
            event_index=base_index,
            ts=ts,
            kind="swap_out",
            asset=out[0],
            qty=out[1],
            raw=raw,
            usd_price=out[2],
            pricing_policy="hyperliquid_fill",
        ),
        EventDraft(
            tx_hash=tx_hash,
            event_index=base_index + 1,
            ts=ts,
            kind="swap_in",
            asset=into[0],
            qty=into[1],
            raw=raw,
            usd_price=into[2],
            pricing_policy="hyperliquid_fill",
        ),
    ]


def normalize_funding(entry: dict[str, Any]) -> list[EventDraft]:
    delta = entry.get("delta") or {}
    amount = _decimal(delta.get("usdc"))
    time_ms = entry.get("time")
    coin = str(delta.get("coin") or "")[:40]
    if delta.get("type") != "funding" or amount is None or amount == 0 or not isinstance(time_ms, int):
        return []
    return [
        EventDraft(
            # o hash de funding costuma vir zerado; ativo + horário identifica o pagamento
            tx_hash=f"funding-{coin}-{time_ms}",
            event_index=0,
            ts=datetime.fromtimestamp(time_ms / 1000, UTC),
            kind="funding",
            asset=USDC,
            qty=abs(amount),
            # positivo = recebido, negativo = pago
            raw={"coin": coin, "usdc": str(amount), "fundingRate": str(delta.get("fundingRate") or "")[:30]},
            usd_price=Decimal(1),
            pricing_policy="stable",
        )
    ]


class HyperliquidClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def _info(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            response = await self._http.post(INFO_URL, json=payload, timeout=20)
        except httpx.HTTPError:
            raise IngestError("source_unavailable") from None
        if response.status_code == 429 or response.status_code >= 500:
            raise IngestError("source_unavailable")
        if response.status_code >= 400:
            raise IngestError("source_rejected")
        data = response.json()
        return data if isinstance(data, list) else []

    async def fills(self, user: str, start_ms: int) -> list[dict[str, Any]]:
        return await self._info({"type": "userFillsByTime", "user": user, "startTime": start_ms})

    async def funding(self, user: str, start_ms: int) -> list[dict[str, Any]]:
        return await self._info({"type": "userFunding", "user": user, "startTime": start_ms})
