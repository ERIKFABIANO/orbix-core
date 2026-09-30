import pytest
from pydantic import TypeAdapter, ValidationError

from orbix.schemas import WalletIn

wallet = TypeAdapter(WalletIn)


@pytest.mark.parametrize(
    "address",
    [
        "11111111111111111111111111111111",  # System Program
        "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr",  # Memo Program
    ],
)
def test_valid_solana(address: str) -> None:
    assert wallet.validate_python({"chain": "solana", "address": address}).address == address


@pytest.mark.parametrize(
    "address",
    [
        "",
        "0OIl0OIl0OIl0OIl0OIl0OIl0OIl0OIl",  # caracteres fora do base58
        "abc",
        "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHrXXXXXXXXXX",
        "<script>alert(1)</script>",
    ],
)
def test_invalid_solana(address: str) -> None:
    with pytest.raises(ValidationError):
        wallet.validate_python({"chain": "solana", "address": address})


def test_valid_hyperliquid() -> None:
    address = "0x" + "a" * 40
    assert wallet.validate_python({"chain": "hyperliquid", "address": address}).address == address


@pytest.mark.parametrize("address", ["0x123", "a" * 42, "0x" + "g" * 40])
def test_invalid_hyperliquid(address: str) -> None:
    with pytest.raises(ValidationError):
        wallet.validate_python({"chain": "hyperliquid", "address": address})


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        wallet.validate_python(
            {"chain": "solana", "address": "11111111111111111111111111111111", "user_id": "x"}
        )
