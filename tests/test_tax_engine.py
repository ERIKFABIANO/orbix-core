from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import count
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from orbix.tax.engine import Event, compute_rows, money, month_of, summarize

LIMIT, RATE = Decimal("35000"), Decimal("0.15")
_ids = count(1)
T0 = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)


def ev(
    kind: str,
    asset: str,
    qty: str,
    brl: str | None,
    *,
    tx: str | None = None,
    ts: datetime = T0,
    wallet: str = "w1",
    chain: str = "solana",
    raw: dict[str, Any] | None = None,
    ptax: str | None = "5",
    policy: str | None = "auto",
) -> Event:
    n = next(_ids)
    return Event(
        id=f"e{n}",
        wallet_id=wallet,
        chain=chain,
        tx_hash=tx or f"tx{n}",
        ts=ts,
        kind=kind,
        asset=asset,
        qty=Decimal(qty),
        brl_value=Decimal(brl) if brl is not None else None,
        ptax=Decimal(ptax) if ptax else None,
        policy=policy,
        raw=raw or {},
        symbol=asset,
    )


def swap(sell: tuple[str, str, str | None], buy: tuple[str, str, str | None], **kw: Any) -> list[Event]:
    tx = f"swap{next(_ids)}"
    return [ev("swap_out", *sell, tx=tx, **kw), ev("swap_in", *buy, tx=tx, **kw)]


def test_average_cost_and_gain() -> None:
    events = [
        *swap(("USDC", "1000", "5000"), ("SOL", "10", "5000")),  # compra 10 SOL a R$ 500
        *swap(("USDC", "3000", "15000"), ("SOL", "10", "15000")),  # compra 10 SOL a R$ 1.500
        *swap(
            ("SOL", "5", "10000"), ("USDC", "2000", "10000"), ts=T0 + timedelta(days=1)
        ),  # vende 5 a R$ 2.000
    ]
    rows = compute_rows(events)
    sale = rows[-1]
    assert sale.asset == "SOL → USDC"
    assert sale.value == Decimal("10000")
    assert sale.cost == Decimal("5000")  # custo médio R$ 1.000 x 5
    assert sale.gain == Decimal("5000")
    assert (sale.quantity, sale.quantity_asset) == (Decimal("5"), "SOL")


def test_multi_fill_sale_sums_quantity_not_just_the_first_leg() -> None:
    """Duas pernas de venda do mesmo ativo numa só transação (uma ordem em dois fills, como a
    Hyperliquid faz): a quantidade da linha soma as duas pernas, não só a primeira. Sem isso o
    custo por unidade parecia quase o dobro do real (relatório de testes de 06/10, B6)."""
    events = [*swap(("USDC", "1243", "6220"), ("UBTC", "0.01243", "6220"))]  # compra 0,01243 UBTC
    tx = f"swap{next(_ids)}"
    events += [
        ev("swap_out", "UBTC", "0.00793", "4000", tx=tx),
        ev("swap_out", "UBTC", "0.00449", "2264", tx=tx),
        ev("swap_in", "USDC", "6264", "6264", tx=tx),
    ]
    rows = compute_rows(events)
    sale = rows[-1]
    assert sale.quantity == Decimal("0.01242")
    # custo das duas pernas somadas, proporcional à posição inteira (não só à primeira perna)
    assert money(sale.cost) == money(Decimal("6220") * Decimal("0.01242") / Decimal("0.01243"))
    # a perna sozinha, só pra deixar claro o que o bug mostrava: bem menos do que o custo real
    assert money(sale.cost) != money(Decimal("6220") * Decimal("0.00793") / Decimal("0.01243"))


def test_hyperliquid_fee_in_usdc_is_captured_from_the_fill() -> None:
    """A Hyperliquid manda fee/feeToken em cada fill, não num evento de taxa à parte; sem
    isso o campo de taxas aparecia como indisponível (relatório de testes de 06/10, B4)."""
    tx = f"swap{next(_ids)}"
    sell = ev("swap_out", "SOL", "5", "2500", tx=tx, raw={"fee": "1.5", "feeToken": "USDC"})
    buy = ev("swap_in", "USDC", "2500", "2500", tx=tx)
    rows = compute_rows([sell, buy])
    assert rows[-1].fees == Decimal("1.5") * Decimal("5")  # taxa vezes a PTAX da linha

    # taxa paga no próprio ativo negociado: sem preço dele aqui, fica indisponível (não inventa)
    tx2 = f"swap{next(_ids)}"
    sell2 = ev("swap_out", "SOL", "5", "2500", tx=tx2, raw={"fee": "0.01", "feeToken": "SOL"})
    buy2 = ev("swap_in", "USDC", "2500", "2500", tx=tx2)
    rows2 = compute_rows([sell2, buy2])
    assert rows2[-1].fees is None


def test_stablecoin_purchase_is_a_disposal_of_the_stablecoin_at_cost() -> None:
    events = [
        ev("transfer_in", "USDC", "1000", "5000"),
        *swap(("USDC", "1000", "5000"), ("SOL", "10", "5000"), ts=T0 + timedelta(hours=1)),
    ]
    row = compute_rows(events)[-1]
    assert (row.value, row.cost, row.gain) == (Decimal("5000"), Decimal("5000"), Decimal("0"))


def test_one_priced_leg_prices_the_whole_swap() -> None:
    events = [
        ev("transfer_in", "MEME", "1000", "100"),
        *swap(("MEME", "1000", None), ("USDC", "50", "250"), ts=T0 + timedelta(hours=1)),
    ]
    row = compute_rows(events)[-1]
    assert (row.value, row.cost, row.gain) == (Decimal("250"), Decimal("100"), Decimal("150"))


def test_unpriced_swap_counts_as_missing_price() -> None:
    rows = compute_rows(swap(("MEME", "10", None), ("OTHER", "3", None)))
    assert rows[0].priced is False
    totals = summarize(rows, LIMIT, RATE)
    assert (totals.missing_prices, totals.rows, totals.gain) == (1, 0, Decimal(0))


def test_selling_more_than_known_history_uses_zero_cost_for_the_rest() -> None:
    events = [
        ev("transfer_in", "SOL", "1", "500"),
        *swap(("SOL", "3", "1800"), ("USDC", "360", "1800"), ts=T0 + timedelta(hours=1)),
    ]
    row = compute_rows(events)[-1]
    assert (row.cost, row.gain) == (Decimal("500"), Decimal("1300"))
    assert row.cost_unknown is True

    # com a política "market", a parte sem origem entra pelo valor da venda: 2/3 de 1.800
    market = compute_rows(events, "market")[-1]
    assert (market.cost, market.gain) == (Decimal("1700"), Decimal("100"))
    assert market.cost_unknown is True


def test_fees_and_staking_do_not_create_rows() -> None:
    events = [
        ev("fee", "SOL", "0.01", "5"),
        ev("stake", "SOL", "5", "2500"),
        ev("unstake", "SOL", "5", "2600"),
        ev("transfer_out", "SOL", "2", "1000"),
    ]
    assert compute_rows(events) == []


def test_deposits_and_rewards_show_up_but_are_not_taxed() -> None:
    """Entrada recebida não gera imposto, mas precisa aparecer — senão o usuário não tem como
    ver que a carteira foi lida (relatório de testes de 06/10, B9)."""
    events = [ev("transfer_in", "SOL", "10", "5000"), ev("reward", "SOL", "0.1", "50")]
    rows = compute_rows(events)
    assert [(r.type, r.reportable, r.quantity) for r in rows] == [
        ("transfer", False, Decimal("10")),
        ("transfer", False, Decimal("0.1")),
    ]
    # não entra no total nem conta como pendência, mesmo sem reportable
    totals = summarize(rows, LIMIT, RATE)
    assert (totals.rows, totals.missing_prices, totals.disposed) == (0, 0, Decimal("0"))


def test_transfer_between_own_wallets_keeps_cost_basis() -> None:
    events = [
        *swap(("USDC", "1000", "5000"), ("SOL", "10", "5000")),
        ev("transfer_out", "SOL", "10", "9000", tx="move", wallet="w1", ts=T0 + timedelta(hours=1)),
        ev("transfer_in", "SOL", "10", "9000", tx="move", wallet="w2", ts=T0 + timedelta(hours=1)),
        *swap(("SOL", "10", "9000"), ("USDC", "1800", "9000"), wallet="w2", ts=T0 + timedelta(hours=2)),
    ]
    sale = compute_rows(events)[-1]
    # o custo continua o da compra original (R$ 5.000), não o valor de mercado na transferência
    assert (sale.cost, sale.gain) == (Decimal("5000"), Decimal("4000"))


def test_perp_close_uses_realized_pnl_minus_fees() -> None:
    hl = {"chain": "hyperliquid"}
    events = [
        ev("perp_fill", "HYPE", "10", "2000", raw={"closedPnl": "0", "fee": "0.2"}, **hl),
        ev(
            "perp_fill",
            "HYPE",
            "10",
            "2200",
            raw={"closedPnl": "40", "fee": "0.3"},
            ts=T0 + timedelta(hours=1),
            **hl,
        ),
    ]
    opening, closing = compute_rows(events)
    assert opening.reportable is False
    assert closing.reportable is True
    assert closing.asset == "HYPE-PERP"
    assert closing.gain == Decimal("197.5")  # (40 - 0,2 - 0,3) x PTAX 5
    assert closing.value == Decimal("2200")
    assert closing.cost == Decimal("2002.5")


def test_funding_received_is_gain_and_paid_is_cost() -> None:
    hl = {"chain": "hyperliquid"}
    received, paid = compute_rows(
        [
            ev("funding", "USDC", "2", "10", raw={"usdc": "2", "coin": "HYPE"}, **hl),
            ev("funding", "USDC", "1", "5", raw={"usdc": "-1", "coin": "BTC"}, **hl),
        ]
    )
    assert (received.value, received.cost, received.gain) == (Decimal("10"), Decimal("0"), Decimal("10"))
    assert (paid.value, paid.cost, paid.gain) == (Decimal("0"), Decimal("5"), Decimal("-5"))
    assert received.asset == "HYPE-PERP"


def test_exemption_applies_only_to_spot_under_the_limit() -> None:
    small = compute_rows(
        [
            ev("transfer_in", "SOL", "10", "10000"),
            *swap(("SOL", "10", "30000"), ("USDC", "6000", "30000"), ts=T0 + timedelta(hours=1)),
        ]
    )
    assert summarize(small, LIMIT, RATE).tax == Decimal(0)

    large = compute_rows(
        [
            ev("transfer_in", "SOL", "10", "10000"),
            *swap(("SOL", "10", "40000"), ("USDC", "8000", "40000"), ts=T0 + timedelta(hours=1)),
        ]
    )
    totals = summarize(large, LIMIT, RATE)
    assert totals.gain == Decimal("30000")
    assert totals.tax == Decimal("4500.00")
    assert (totals.disposals, totals.spot_disposed) == (1, Decimal("40000"))


def test_derivative_gain_is_taxed_even_under_the_spot_limit() -> None:
    rows = compute_rows(
        [ev("funding", "USDC", "100", "500", raw={"usdc": "100", "coin": "HYPE"}, chain="hyperliquid")]
    )
    assert summarize(rows, LIMIT, RATE).tax == Decimal("75.00")


def test_losses_never_produce_negative_tax() -> None:
    rows = compute_rows(
        [
            ev("transfer_in", "SOL", "10", "50000"),
            *swap(("SOL", "10", "40000"), ("USDC", "8000", "40000"), ts=T0 + timedelta(hours=1)),
        ]
    )
    totals = summarize(rows, LIMIT, RATE)
    assert totals.gain == Decimal("-10000")
    assert totals.tax == Decimal(0)


def test_month_follows_brasilia_time() -> None:
    # 01/10 às 01h UTC ainda é 30/09 em Brasília
    assert month_of(datetime(2026, 10, 1, 1, 0, tzinfo=UTC)) == "2026-09"
    assert month_of(datetime(2026, 10, 1, 4, 0, tzinfo=UTC)) == "2026-10"


def test_money_rounds_half_up() -> None:
    assert money(Decimal("1.005")) == 1.01
    assert money(Decimal("-0.004")) == 0.0
    assert money(None) == 0.0


amounts = st.decimals(
    min_value=Decimal("0.0001"), max_value=Decimal("100000"), places=4, allow_nan=False, allow_infinity=False
)
values = st.decimals(
    min_value=Decimal("0.01"), max_value=Decimal("1000000"), places=2, allow_nan=False, allow_infinity=False
)


@given(st.lists(st.tuples(amounts, values), min_size=1, max_size=12), values)
def test_selling_everything_realizes_exactly_the_total_cost(
    buys: list[tuple[Decimal, Decimal]], proceeds: Decimal
) -> None:
    events = [
        ev("transfer_in", "SOL", str(q), str(v), ts=T0 + timedelta(minutes=i))
        for i, (q, v) in enumerate(buys)
    ]
    total_qty = sum((q for q, _ in buys), Decimal(0))
    total_cost = sum((v for _, v in buys), Decimal(0))
    events += swap(
        ("SOL", str(total_qty), str(proceeds)), ("USDC", "1", str(proceeds)), ts=T0 + timedelta(days=1)
    )
    row = compute_rows(events)[-1]
    assert abs(row.cost - total_cost) < Decimal("0.000001")
    assert abs(row.gain - (proceeds - total_cost)) < Decimal("0.000001")


@given(st.lists(st.tuples(amounts, values), min_size=1, max_size=10), st.integers(1, 99))
def test_partial_sale_cost_stays_between_zero_and_total(
    buys: list[tuple[Decimal, Decimal]], pct: int
) -> None:
    events = [
        ev("transfer_in", "SOL", str(q), str(v), ts=T0 + timedelta(minutes=i))
        for i, (q, v) in enumerate(buys)
    ]
    total_qty = sum((q for q, _ in buys), Decimal(0))
    total_cost = sum((v for _, v in buys), Decimal(0))
    sold = (total_qty * pct / 100).quantize(Decimal("0.00000001"))
    if sold <= 0:
        return
    events += swap(("SOL", str(sold), "1000"), ("USDC", "200", "1000"), ts=T0 + timedelta(days=1))
    row = compute_rows(events)[-1]
    assert Decimal(0) <= row.cost <= total_cost + Decimal("0.000001")
