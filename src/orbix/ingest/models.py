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


# Na Hyperliquid o ativo spot é o código do par ("@230"); o nome só chega pelo spotMeta.
# Estes são os dólares que a própria Hyperliquid usa como moeda de cotação.
HYPERLIQUID_STABLE_SYMBOLS = frozenset({"USDC", "USDH", "USDT0", "USDE"})


def is_stable(chain: str, asset: str, symbol: str | None = None) -> bool:
    if asset in STABLE_ASSETS.get(chain, frozenset()):
        return True
    return chain == "hyperliquid" and (symbol or "").upper() in HYPERLIQUID_STABLE_SYMBOLS


def position_token(chain: str, asset: str, symbol: str | None) -> str | None:
    """Token a que um ativo da Hyperliquid se refere, para o custo médio.

    Na Hyperliquid o fill grava o código do par ("@334" = KNTQ/USDC, "@254" = KNTQ/USDH) e o
    extrato grava o nome do token ("KNTQ"). São o mesmo ativo: sem juntar, comprar num par e
    vender no outro, ou receber por transferência e vender, saía com custo zero. Devolve None
    quando o ativo já é a própria identidade (Solana, ou par ainda sem nome resolvido)."""
    if chain != "hyperliquid":
        return None
    if asset.startswith("@"):
        return symbol or None
    if "/" in asset:
        return asset.split("/", 1)[0]
    return None


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
