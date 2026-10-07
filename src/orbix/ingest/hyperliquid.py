"""Fills e funding da Hyperliquid. A API de informação é pública, sem chave."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from orbix.ingest.models import EventDraft, IngestError

INFO_URL = "https://api.hyperliquid.xyz/info"
USDC = "USDC"
PAGE_LIMIT = 2000
# A Hyperliquid zera o hash de alguns fills (conversão de poeira e outros eventos internos).
# Tratar essa string como se fosse um hash de verdade funde fills de pares diferentes num só
# evento (ver relatório de testes de 06/10, B1 e B7).
_ZERO_HASH = "0x" + "0" * 64


def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _is_spot(coin: str) -> bool:
    return coin.startswith("@") or "/" in coin


def _fill_key(fill: dict[str, Any], coin: str, time_ms: int) -> str:
    """Identifica a que ordem este fill pertence, para agrupar só fills do mesmo par e ordem.

    Prioriza o hash real da Hyperliquid (vale como identidade da transação). Quando ele vem
    zerado, agrupa por ordem (oid) + par: é o que a própria Hyperliquid usa para dizer que
    vários fills pertencem à mesma execução. Sem oid (raríssimo), cai no id do fill ou no
    instante, nunca compartilhando chave entre pares diferentes.
    """
    raw_hash = fill.get("hash")
    if isinstance(raw_hash, str) and raw_hash and raw_hash != _ZERO_HASH:
        return raw_hash[:100]
    oid = fill.get("oid")
    if isinstance(oid, int) and oid > 0:
        return f"hl:{coin}:{oid}"[:100]
    tid = fill.get("tid")
    tid_part = tid if isinstance(tid, int) and tid > 0 else time_ms
    return f"hl:{coin}:t{tid_part}"[:100]


def normalize_fill(fill: dict[str, Any]) -> list[EventDraft]:
    coin = str(fill.get("coin") or "")[:40]
    price, size = _decimal(fill.get("px")), _decimal(fill.get("sz"))
    time_ms, tid = fill.get("time"), fill.get("tid")
    if not coin or price is None or size is None or size <= 0 or not isinstance(time_ms, int):
        return []
    ts = datetime.fromtimestamp(time_ms / 1000, UTC)
    tx_hash = _fill_key(fill, coin, time_ms)
    # vários fills podem dividir a mesma chave (mesma ordem); o id do fill torna o índice
    # único e estável dentro do grupo
    base_index = (int(tid) % 1_000_000_000) * 2 if isinstance(tid, int) else 0
    side = str(fill.get("side") or "")
    raw: dict[str, Any] = {
        "side": side,
        "dir": str(fill.get("dir") or "")[:30],
        "px": str(price),
        "closedPnl": str(_decimal(fill.get("closedPnl")) or Decimal(0)),
        "fee": str(_decimal(fill.get("fee")) or Decimal(0)),
        "feeToken": str(fill.get("feeToken") or USDC)[:20],
        # par e ordem: o motor fiscal junta os fills da mesma ordem numa linha só
        "coin": coin,
    }
    oid = fill.get("oid")
    if isinstance(oid, int) and oid > 0:
        raw["oid"] = oid
    if isinstance(tid, int) and tid > 0:
        raw["tid"] = tid

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

    async def spot_meta_names(self) -> dict[str, str]:
        """Nome do token base de cada par spot sem nome ('@107' etc.), pela lista pública
        spotMeta. Pares já nomeados (ex.: PURR/USDC) não precisam disso."""
        try:
            response = await self._http.post(INFO_URL, json={"type": "spotMeta"}, timeout=20)
        except httpx.HTTPError:
            return {}
        if response.status_code >= 400:
            return {}
        data = response.json()
        if not isinstance(data, dict):
            return {}
        tokens = {
            t.get("index"): str(t.get("name") or "") for t in data.get("tokens", []) if isinstance(t, dict)
        }
        names: dict[str, str] = {}
        for pair in data.get("universe", []):
            if not isinstance(pair, dict):
                continue
            pair_name = str(pair.get("name") or "")
            legs = pair.get("tokens")
            if not pair_name.startswith("@") or not isinstance(legs, list) or not legs:
                continue
            base = tokens.get(legs[0])
            if base:
                names[pair_name] = base
        return names
