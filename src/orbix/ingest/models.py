from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

SOL = "SOL"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"

# valem 1 dólar por definição; não dependem de fonte de preço
STABLE_ASSETS: dict[str, frozenset[str]] = {
    "solana": frozenset(
        {
            USDC_MINT,
            USDT_MINT,
            "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo",  # PYUSD
            "USDSwr9ApdHk5bvJKMjzff41FfuX8bSxdKcR81vTwcA",  # USDS
            "2u1tszSeqZ3qBWF3uNGPFc8TzMk2tdiwknnRMWGWjGWH",  # USDG
        }
    ),
    "hyperliquid": frozenset({"USDC"}),
}

KNOWN_SYMBOLS: dict[str, str] = {
    SOL: "SOL",
    USDC_MINT: "USDC",
    USDT_MINT: "USDT",
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": "JitoSOL",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": "mSOL",
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": "bSOL",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN": "JUP",
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263": "BONK",
    "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm": "WIF",
    "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL": "JTO",
    "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3": "PYTH",
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo": "PYUSD",
}


def is_stable(chain: str, asset: str) -> bool:
    return asset in STABLE_ASSETS.get(chain, frozenset())


def short(asset: str) -> str:
    return asset if len(asset) <= 12 else f"{asset[:4]}…{asset[-4:]}"


@dataclass(frozen=True)
class EventDraft:
    tx_hash: str
    event_index: int
    ts: datetime
    kind: str
    asset: str
    qty: Decimal
    raw: dict[str, Any] = field(default_factory=dict)
    # preenchido quando a própria fonte já traz o preço (fills da Hyperliquid)
    usd_price: Decimal | None = None
    pricing_policy: str | None = None


class IngestError(Exception):
    """Falha de leitura numa fonte. `code` é traduzido para o usuário em i18n.SYNC_ERRORS."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
