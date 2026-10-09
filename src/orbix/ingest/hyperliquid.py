"""Fills e funding da Hyperliquid. A API de informação é pública, sem chave."""

import re
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


_ADDRESS = re.compile(r"^0x[0-9a-f]{40}$")
# endereços de sistema da ponte HyperEVM <-> HyperCore: 0x20, zeros e o índice do token no fim
_BRIDGE = re.compile(r"^0x20(?:0{30})[0-9a-f]{8}$")


def _ledger_asset(token: Any) -> str | None:
    """O extrato identifica o ativo pelo nome do token ("KNTQ"); os fills, pelo código do par
    ("@334"). O motor fiscal junta os dois pela posição do token (ver `position_token`)."""
    name = str(token or USDC).strip()[:40]
    return name or None


def normalize_ledger(entry: dict[str, Any], wallet: str) -> list[EventDraft]:
    """Movimentos da conta que não são execução nem funding: depósito, saque, transferência
    de token, airdrop, recompensa e staking (`userNonFundingLedgerUpdates`).

    Sem eles, um token que entrou por transferência ou airdrop era vendido sem a entrada
    correspondente e saía com custo zero (relatório de testes de 08/10, B17).
    """
    delta = entry.get("delta") or {}
    kind_in = str(delta.get("type") or "")
    time_ms = entry.get("time")
    if not kind_in or not isinstance(time_ms, int):
        return []
    ts = datetime.fromtimestamp(time_ms / 1000, UTC)
    raw_hash = entry.get("hash")
    tx_hash = (
        raw_hash[:100]
        if isinstance(raw_hash, str) and raw_hash and raw_hash != _ZERO_HASH
        else f"ledger-{kind_in[:30]}-{time_ms}"
    )
    me = wallet.lower()
    raw: dict[str, Any] = {"ledger": kind_in[:30]}

    def draft(
        kind: str,
        asset: str,
        qty: Decimal,
        usd_price: Decimal | None,
        *,
        other: str | None = None,
        index: int = 0,
    ) -> EventDraft:
        stable = asset == USDC
        data = {**raw, "counterparty": other} if other and _ADDRESS.match(other) else raw
        return EventDraft(
            tx_hash=tx_hash,
            event_index=index,
            ts=ts,
            kind=kind,
            asset=asset,
            qty=qty,
            raw=data,
            usd_price=Decimal(1) if stable else usd_price,
            pricing_policy="stable" if stable else "hyperliquid_ledger" if usd_price is not None else None,
        )

    if kind_in in ("deposit", "withdraw"):
        amount = _decimal(delta.get("usdc"))
        if amount is None or amount <= 0:
            return []
        if kind_in == "withdraw":
            raw["fee"] = str(_decimal(delta.get("fee")) or Decimal(0))
        return [draft("transfer_in" if kind_in == "deposit" else "transfer_out", USDC, amount, Decimal(1))]

    if kind_in in ("spotTransfer", "send", "internalTransfer"):
        sender = str(delta.get("user") or "").lower()
        receiver = str(delta.get("destination") or "").lower()
        token = USDC if kind_in == "internalTransfer" else delta.get("token")
        amount = _decimal(delta.get("usdc") if kind_in == "internalTransfer" else delta.get("amount"))
        asset = _ledger_asset(token)
        # movimento entre contas da própria carteira (spot e perpétuos) não é entrada nem saída
        if asset is None or amount is None or amount <= 0 or sender == receiver:
            return []
        if me not in (sender, receiver):
            return []
        value = _decimal(delta.get("usdcValue"))
        price = value / amount if value is not None and value > 0 else None
        raw["token"] = str(token or USDC)[:40]
        if value is not None:
            raw["usdcValue"] = str(value)
        if receiver == me:
            # endereço de sistema da ponte: o token veio da HyperEVM e já era da própria pessoa
            if _BRIDGE.match(sender):
                raw["bridge"] = True
            return [draft("transfer_in", asset, amount, price, other=sender)]
        events = [draft("transfer_out", asset, amount, price, other=receiver)]
        fee = _decimal(delta.get("fee"))
        fee_asset = _ledger_asset(delta.get("feeToken")) if delta.get("feeToken") else None
        if fee is not None and fee > 0 and fee_asset is not None:
            # a taxa da transferência sai da carteira no próprio token
            events.append(draft("fee", fee_asset, fee, price if fee_asset == asset else None, index=1))
        return events

    if kind_in in ("spotGenesis", "rewardsClaim"):
        amount = _decimal(delta.get("amount"))
        asset = _ledger_asset(delta.get("token"))
        if asset is None or amount is None or amount <= 0:
            return []
        raw["token"] = str(delta.get("token") or USDC)[:40]
        # airdrop e recompensa: entram na posição; sem preço na fonte, o custo é zero
        return [draft("reward", asset, amount, None)]

    if kind_in == "cStakingTransfer":
        amount = _decimal(delta.get("amount"))
        asset = _ledger_asset(delta.get("token"))
        if asset is None or amount is None or amount <= 0:
            return []
        # staking: o token continua da pessoa, a posição não muda
        return [draft("stake" if delta.get("isDeposit") else "unstake", asset, amount, None)]

    # accountClassTransfer (spot ↔ perpétuos da mesma conta), cofres e demais: não mexem na posição
    return []


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

    async def ledger(self, user: str, start_ms: int) -> list[dict[str, Any]]:
        return await self._info({"type": "userNonFundingLedgerUpdates", "user": user, "startTime": start_ms})

    async def _spot_meta(self) -> dict[str, Any] | None:
        try:
            response = await self._http.post(INFO_URL, json={"type": "spotMeta"}, timeout=20)
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None

    async def spot_meta_names(self) -> dict[str, str]:
        """Nome do token base de cada par spot sem nome ('@107' etc.), pela lista pública
        spotMeta. Pares já nomeados (ex.: PURR/USDC) não precisam disso."""
        data = await self._spot_meta()
        if data is None:
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
