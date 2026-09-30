from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
from solders.pubkey import Pubkey


def _solana_address(value: str) -> str:
    # não checar a curva: endereços de programa (PDA) são válidos e ficam fora dela
    try:
        ok = str(Pubkey.from_string(value)) == value
    except Exception:
        ok = False
    if not ok:
        raise ValueError("endereço Solana inválido")
    return value


SolanaAddress = Annotated[
    str, StringConstraints(min_length=32, max_length=44), AfterValidator(_solana_address)
]
HyperliquidAddress = Annotated[str, StringConstraints(pattern=r"^0x[a-fA-F0-9]{40}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SolanaWallet(StrictModel):
    chain: Literal["solana"]
    address: SolanaAddress


class HyperliquidWallet(StrictModel):
    chain: Literal["hyperliquid"]
    address: HyperliquidAddress


WalletIn = Annotated[SolanaWallet | HyperliquidWallet, Field(discriminator="chain")]
