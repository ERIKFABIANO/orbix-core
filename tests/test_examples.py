"""Teste espelho dos exemplos públicos do front (orbix-declare/examples/).

Mesma entrada, mesmo resultado. Se uma regra do motor mudar e um número mudar aqui, o
exemplo do front muda junto.
"""

from datetime import UTC, datetime
from decimal import Decimal

from orbix.tax.engine import Event, compute_rows

BEFORE = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)


def _ev(n: int, kind: str, asset: str, qty: str, brl: str | None, **kw: object) -> Event:
    base: dict[str, object] = {
        "id": f"x{n}",
        "wallet_id": "w",
        "chain": "solana",
        "tx_hash": f"tx{n}",
        "ts": BEFORE,
        "kind": kind,
        "asset": asset,
        "qty": Decimal(qty),
        "brl_value": Decimal(brl) if brl is not None else None,
        "ptax": Decimal("5.4128"),
        "policy": "auto",
        "raw": {},
        "symbol": asset,
    }
    return Event(**{**base, **kw})  # type: ignore[arg-type]


def test_example_01_swap_with_stablecoin() -> None:
    """12,4 SOL vendidos por USDC: valor R$ 11.284,00, custo médio R$ 9.412,60, ganho R$ 1.871,40."""
    sale_ts = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)
    events = [
        # compra anterior que forma o custo médio: 12,4 SOL por R$ 9.412,60
        _ev(1, "swap_out", "USDC", "1740", "9412.60", tx_hash="buy", stable=True),
        _ev(2, "swap_in", "SOL", "12.4", "9412.60", tx_hash="buy"),
        _ev(3, "swap_out", "SOL", "12.4", "11284.00", tx_hash="sale", ts=sale_ts),
        _ev(4, "swap_in", "USDC", "2084.69", "11284.00", tx_hash="sale", ts=sale_ts, stable=True),
    ]
    row = compute_rows(events)[-1]
    assert (row.asset, row.quantity, row.quantity_asset) == ("SOL → USDC", Decimal("12.4"), "SOL")
    assert (row.value, row.cost, row.gain) == (Decimal("11284.00"), Decimal("9412.60"), Decimal("1871.40"))
    assert row.unit_price == Decimal("910")
    assert (row.cost_unknown, row.cost_manual) == (False, False)
    # os dois lados da troca e a origem do custo
    assert (row.quantity_in, row.quantity_in_asset) == (Decimal("2084.69"), "USDC")
    assert row.position_before_qty == Decimal("12.4")
    assert row.avg_cost_unit is not None and row.avg_cost_unit * row.quantity == row.cost


def test_example_02_perp_close_and_funding() -> None:
    """150 HYPE-PERP fechados: valor R$ 6.912,50, custo R$ 5.980,00, ganho R$ 932,50.
    Funding de 18,42 USDC recebido."""
    hl = {"chain": "hyperliquid", "ptax": Decimal("5.4096")}
    # resultado realizado menos taxa, em dólar, que dá o ganho do exemplo pela PTAX
    net_usd = Decimal("932.50") / Decimal("5.4096")
    events = [
        _ev(
            1,
            "funding",
            "USDC",
            "18.42",
            None,
            ts=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
            raw={"coin": "HYPE", "usdc": "18.42"},
            **hl,
        ),
        _ev(
            2,
            "perp_fill",
            "HYPE",
            "150",
            "6912.50",
            ts=datetime(2026, 9, 26, 15, 0, tzinfo=UTC),
            raw={"closedPnl": str(net_usd + 1), "fee": "1"},
            **hl,
        ),
    ]
    funding, perp = compute_rows(events)
    assert (perp.asset, perp.value, perp.cost, perp.gain) == (
        "HYPE-PERP",
        Decimal("6912.50"),
        Decimal("5980.00"),
        Decimal("932.50"),
    )
    assert perp.unit_price is not None and round(perp.unit_price, 8) == Decimal("46.08333333")
    # funding recebido é ganho inteiro e vale a quantidade em USDC vezes a PTAX: 18,42 x 5,4096.
    # O exemplo do front traz R$ 99,83 (preço unitário 5,4197, diferente da PTAX da linha);
    # pela regra o valor é R$ 99,64 e o preço unitário é a própria PTAX.
    assert (funding.value, funding.cost, funding.gain) == (
        Decimal("99.64"),
        Decimal("0.00"),
        Decimal("99.64"),
    )
    assert funding.unit_price is not None and round(funding.unit_price, 4) == Decimal("5.4096")


def test_example_03_cost_informed_by_the_user() -> None:
    """2.400 JUP vendidos sem compra no histórico: custo zero e ganho R$ 7.416,00, marcado como
    custo desconhecido. Com o custo informado (R$ 6.524,40), o ganho vira R$ 891,60."""
    ts = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
    out = _ev(1, "swap_out", "JUP", "2400", "7416.00", tx_hash="sale", ts=ts)
    into = _ev(2, "swap_in", "SOL", "8.15", "7416.00", tx_hash="sale", ts=ts)

    (before,) = compute_rows([out, into])
    assert (before.cost, before.gain) == (Decimal("0.00"), Decimal("7416.00"))
    assert (before.cost_unknown, before.cost_manual) == (True, False)
    assert before.unit_price == Decimal("3.09")

    informed = Event(**{**out.__dict__, "cost_override": Decimal("6524.40")})
    (after,) = compute_rows([informed, into])
    assert (after.cost, after.gain) == (Decimal("6524.40"), Decimal("891.60"))
    assert (after.cost_unknown, after.cost_manual) == (False, True)
