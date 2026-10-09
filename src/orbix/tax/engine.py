"""Motor fiscal: transforma eventos em linhas de relatório e totais mensais.

Código puro, sem banco nem rede, para ser testável de ponta a ponta.

Regras adotadas (estimativa; não substitui orientação de um contador):
- Custo de aquisição pelo método do custo médio ponderado, por ativo.
- Swap é alienação do que saiu e aquisição do que entrou, pelo valor em reais da operação.
- Transferência recebida e recompensa entram na posição pelo valor de mercado do dia.
- Transferência enviada, taxa de rede e staking não geram ganho; só reduzem ou mantêm a posição.
- Transferência entre duas carteiras do mesmo usuário é ignorada.
- Hyperliquid, extrato da conta: airdrop entra com custo zero; token que chega pela ponte da
  HyperEVM já era do usuário, então o custo não é o valor do dia: entra sem custo comprovado
  e a venda fica marcada até o usuário informar o custo. Dólar (stablecoin) que entra ou sai
  por depósito, saque ou transferência aparece no painel, mas não mexe no custo médio:
  continua valendo o próprio valor na venda, como antes de o extrato ser lido.
- Vender mais do que o histórico conhecido: a parte sem origem tem custo zero (custo não
  comprovado) e a linha é marcada com `cost_unknown`. Stablecoin entra pelo próprio valor.
  Com `unknown_cost="market"`, a parte sem origem também entra pelo valor da venda.
- Perpétuos: o ganho é o resultado realizado (closedPnl) menos as taxas, convertido pela PTAX.
- Funding recebido é ganho; funding pago é custo.
- Isenção: se o total alienado em spot no mês for até o limite, o ganho em spot é isento.
  Ganho com perpétuos e funding não entra na isenção.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal
from zoneinfo import ZoneInfo

BRT = ZoneInfo("America/Sao_Paulo")
ZERO = Decimal(0)
CENT = Decimal("0.01")

RowType = Literal["swap", "perp", "funding", "transfer"]
# custo do que foi vendido sem aquisição conhecida: zero (regra da Receita) ou valor de mercado
UnknownCost = Literal["zero", "market"]


@dataclass(frozen=True)
class Event:
    id: str
    wallet_id: str
    chain: str
    tx_hash: str
    ts: datetime
    kind: str
    asset: str
    qty: Decimal
    brl_value: Decimal | None
    ptax: Decimal | None
    policy: str | None
    raw: dict[str, Any]
    symbol: str
    wallet_address: str = ""
    stable: bool = False
    wallet_label: str | None = None
    price_ts: datetime | None = None
    ptax_date: date | None = None
    # custo de aquisição informado pelo usuário para esta venda (revisão auditada)
    cost_override: Decimal | None = None
    # preço em dólar de uma unidade do ativo, quando a fonte informa
    usd_price: Decimal | None = None
    # token da posição quando o mesmo ativo aparece com códigos diferentes (pares da Hyperliquid)
    position_token: str | None = None


@dataclass
class Row:
    id: str
    ts: datetime
    type: RowType
    chain: str
    asset: str
    quantity: Decimal
    quantity_asset: str
    tx_hash: str
    wallet_address: str
    value: Decimal | None = None
    cost: Decimal = ZERO
    gain: Decimal = ZERO
    ptax: Decimal | None = None
    manual: bool = False
    # entra no relatório e no imposto (abertura de posição em perpétuo não entra)
    reportable: bool = True
    slot: int | None = None
    # parte do que foi vendido entrou antes do histórico lido: custo de aquisição não comprovado
    cost_unknown: bool = False
    # detalhes para a tela de revisão do evento
    wallet_label: str | None = None
    protocol: str | None = None
    fees: Decimal | None = None
    policy: str | None = None
    price_ts: datetime | None = None
    ptax_date: date | None = None
    # custo de aquisição informado pelo usuário em vez do calculado
    cost_manual: bool = False
    # quantos fills da corretora formam esta linha (uma ordem pode executar em vários)
    fills: int = 1
    # o outro lado da troca: o que entrou na carteira
    quantity_in: Decimal | None = None
    quantity_in_asset: str | None = None
    # de onde veio o custo: posição do ativo antes da venda e custo médio por unidade usado
    position_before_qty: Decimal | None = None
    avg_cost_unit: Decimal | None = None
    # valor por unidade antes de arredondar a linha para centavos
    unit_price: Decimal | None = None
    # transferência: entrada ou saída, e o endereço do outro lado quando a fonte informa
    direction: Literal["in", "out"] | None = None
    counterparty: str | None = None

    @property
    def priced(self) -> bool:
        return self.value is not None

    @property
    def month(self) -> str:
        return month_of(self.ts)


@dataclass
class Totals:
    disposed: Decimal = ZERO
    cost: Decimal = ZERO
    gain: Decimal = ZERO
    tax: Decimal = ZERO
    spot_disposed: Decimal = ZERO
    disposals: int = 0
    missing_prices: int = 0
    rows: int = 0


@dataclass
class _Position:
    qty: Decimal = ZERO
    cost: Decimal = ZERO
    # parte da quantidade que entrou sem custo comprovado (veio de outra rede do próprio usuário)
    unproven: Decimal = ZERO

    def add(self, qty: Decimal, cost: Decimal, *, unproven: bool = False) -> None:
        self.qty += qty
        self.cost += cost
        if unproven:
            self.unproven += qty

    def remove(self, qty: Decimal) -> Decimal:
        """Tira `qty` da posição e devolve o custo médio correspondente."""
        if self.qty <= 0 or qty <= 0:
            return ZERO
        taken = min(qty, self.qty)
        cost = self.cost * taken / self.qty
        self.unproven -= self.unproven * taken / self.qty
        self.qty -= taken
        self.cost -= cost
        return cost


@dataclass
class _State:
    positions: dict[str, _Position] = field(default_factory=lambda: defaultdict(_Position))
    perp_fees: dict[str, Decimal] = field(default_factory=lambda: defaultdict(Decimal))


def month_of(moment: datetime) -> str:
    return moment.astimezone(BRT).strftime("%Y-%m")


def money(value: Decimal | None) -> float:
    return float((value or ZERO).quantize(CENT, rounding=ROUND_HALF_UP))


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return ZERO


def _key(event: Event) -> str:
    return f"{event.chain}:{event.position_token or event.asset}"


def _label(events: list[Event]) -> str:
    return " + ".join(dict.fromkeys(e.symbol for e in events))


def _total_qty(events: list[Event]) -> Decimal:
    """Soma a quantidade só quando todas as pernas são do mesmo ativo (uma ordem executada
    em vários fills, por exemplo). Pernas de ativos diferentes (rota com várias etapas) não
    têm uma grandeza única para somar; mantém a da primeira, como antes.

    Sem isso, uma venda de 0,00793 + 0,00449 do mesmo ativo (uma ordem, dois fills) aparecia
    com quantidade 0,00793 mas custo/ganho da soma das duas pernas — custo por unidade quase
    o dobro do real (relatório de testes de 06/10, B6)."""
    if len({e.asset for e in events}) == 1:
        return sum((e.qty for e in events), ZERO)
    return events[0].qty


def _exact_unit(events: list[Event]) -> Decimal | None:
    """Preço em reais de uma unidade: preço em dólar vezes a PTAX, ponderado pela quantidade.

    O valor em reais do evento fica guardado em centavos; dividir esse valor pela quantidade
    distorce o preço quando a quantidade é pequena (0,14 / 0,026225 dá 5,34 em vez da PTAX
    de 5,1495). Devolve None quando falta o preço em dólar ou a PTAX de alguma perna, ou
    quando as pernas são de ativos diferentes."""
    if not events or len({e.asset for e in events}) != 1:
        return None
    if any(e.usd_price is None or e.ptax is None or e.policy == "manual" for e in events):
        return None
    total = sum((e.qty for e in events), ZERO)
    if total <= 0:
        return None
    return sum((e.qty * (e.usd_price or ZERO) * (e.ptax or ZERO) for e in events), ZERO) / total


def _slot(event: Event) -> int | None:
    slot = event.raw.get("slot")
    return slot if isinstance(slot, int) else None


def _protocol(event: Event) -> str | None:
    if event.chain == "hyperliquid":
        return "Hyperliquid"
    source = str(event.raw.get("source") or "")
    if not source or source in ("UNKNOWN", "SYSTEM_PROGRAM"):
        return None
    return source.replace("_", " ").title()


# dólares usados como moeda de cotação na Hyperliquid: a taxa cobrada neles já está em dólar
_USD_FEE_TOKENS = frozenset({"USDC", "USDH", "USDT0", "USDE"})


def _received(leg: Event) -> Decimal:
    """Quantidade que entrou de fato na carteira numa compra.

    Na Hyperliquid a taxa de uma compra spot é cobrada no próprio ativo comprado: quem compra
    10 HYPE com taxa de 0,007 HYPE recebe 9,993. Somar os 10 deixava a posição maior que o
    saldo real (relatório de testes de 09/10, B18). O custo continua sendo o valor pago
    inteiro, então a taxa passa a fazer parte do custo de aquisição do que chegou.

    Stablecoin não muda: a taxa de uma venda é cobrada no dólar recebido, e descontá-la
    mexeria no custo de todo dólar gasto depois. Sem o nome do token (par ainda sem nome
    resolvido) ou com outra moeda de taxa, vale a quantidade do fill."""
    if leg.chain != "hyperliquid" or leg.stable:
        return leg.qty
    token = str(leg.raw.get("feeToken") or "")
    if not token or token != (leg.position_token or leg.symbol):
        return leg.qty
    fee = _decimal(leg.raw.get("fee"))
    # taxa negativa é devolução ao formador de mercado: entra um pouco mais
    return leg.qty - fee if fee != 0 and leg.qty - fee > 0 else leg.qty


def _leg_fees_brl(outs: list[Event]) -> Decimal | None:
    """Taxa de cada fill quando vem no próprio evento (a Hyperliquid manda `fee`/`feeToken`
    em cada fill, em vez de um evento de taxa à parte).

    Recebe só as pernas de saída: cada fill gera uma saída e uma entrada com o mesmo `raw`,
    e somar as duas contaria a taxa em dobro. Taxa em dólar converte direto pela PTAX; taxa
    cobrada no ativo comprado (o normal numa compra spot) usa o preço do próprio fill."""
    total = ZERO
    found = False
    for leg in outs:
        amount = leg.raw.get("fee")
        if amount is None or leg.ptax is None:
            continue
        token = str(leg.raw.get("feeToken") or "")
        if token in _USD_FEE_TOKENS:
            usd = _decimal(amount)
        elif leg.chain == "hyperliquid" and leg.raw.get("px") is not None:
            usd = _decimal(amount) * _decimal(leg.raw.get("px"))
        else:
            continue
        total += usd * leg.ptax
        found = True
    return total if found else None


def _order_key(event: Event) -> str:
    """Chave de agrupamento dentro da carteira.

    Na Hyperliquid uma ordem executa em vários fills, cada um com o próprio hash (ou com o
    hash zerado). Agrupar pelo hash separa a mesma ordem em várias linhas iguais e, no hash
    zerado, junta pares que não têm relação. A ordem (oid) no par (coin) é a identidade
    certa; o dia em Brasília entra na chave porque uma ordem limitada pode executar ao longo
    de vários dias, com PTAX e mês diferentes."""
    oid, coin = event.raw.get("oid"), event.raw.get("coin")
    if event.chain == "hyperliquid" and oid and coin and event.kind in ("swap_in", "swap_out", "perp_fill"):
        return f"hl:{coin}:{oid}:{event.ts.astimezone(BRT).date().isoformat()}"
    return event.tx_hash


_DUST = Decimal("0.000000000001")


def _off_position(event: Event) -> bool:
    """Dólar movimentado pelo extrato da Hyperliquid (depósito, saque, transferência): fica
    visível, mas não entra no custo médio. O dólar comprado com ele continua saindo pelo
    próprio valor, como antes de o extrato ser lido."""
    return event.stable and bool(event.raw.get("ledger"))


def _is_real_hash(tx_hash: str) -> bool:
    return not tx_hash.startswith("hl:") and set(tx_hash.removeprefix("0x")) != {"0"}


def _display_hash(events: list[Event]) -> str:
    """Hash que dá para abrir no explorador, quando algum fill do grupo tem um."""
    return next((e.tx_hash for e in events if _is_real_hash(e.tx_hash)), events[0].tx_hash)


def _merge_perp_rows(rows: list[Row]) -> list[Row]:
    """Junta os fills de uma mesma ordem de perpétuo. Abertura e fechamento ficam em linhas
    separadas: só o fechamento entra no relatório."""
    merged: dict[bool, Row] = {}
    for row in rows:
        kept = merged.get(row.reportable)
        if kept is None:
            merged[row.reportable] = row
            continue
        kept.quantity += row.quantity
        kept.value = None if kept.value is None or row.value is None else kept.value + row.value
        kept.cost += row.cost
        kept.gain += row.gain
        kept.fees = None if kept.fees is None or row.fees is None else kept.fees + row.fees
        kept.manual = kept.manual or row.manual
        kept.fills += row.fills
        kept.unit_price = None
        if not _is_real_hash(kept.tx_hash) and _is_real_hash(row.tx_hash):
            kept.tx_hash = row.tx_hash
    return list(merged.values())


def _to_cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _round_row(row: Row) -> None:
    """Valor e custo em centavos, e o ganho como a diferença dos dois. A posição continua com
    a precisão cheia; só a linha é arredondada. Assim o total do mês é a soma exata das
    linhas e o arquivo exportado fecha sem diferença de centavo."""
    if row.value is None:
        return
    # o preço exato (dólar vezes PTAX) só vale se reproduz o valor da linha; se o preço em dólar
    # guardado não é o que formou o valor (outra perna do swap, por exemplo), fica valor / quantidade
    exact = row.unit_price
    if exact is not None and row.type != "funding" and abs(exact * row.quantity - row.value) > CENT:
        exact = None
    row.unit_price = exact if exact is not None else (row.value / row.quantity if row.quantity else None)
    row.value = _to_cents(row.value)
    if row.type == "perp" and not row.reportable:
        row.cost = _to_cents(row.cost)
        return
    row.cost = _to_cents(row.cost)
    row.gain = row.value - row.cost


def _swap_row(
    state: _State, outs: list[Event], ins: list[Event], unknown_cost: UnknownCost, fees: Decimal | None
) -> Row:
    out_known = all(e.brl_value is not None for e in outs)
    in_known = all(e.brl_value is not None for e in ins)
    out_total = sum((e.brl_value or ZERO for e in outs), ZERO) if out_known else None
    in_total = sum((e.brl_value or ZERO for e in ins), ZERO) if in_known else None
    # o que saiu vale o que entrou: se só um lado tem preço, ele vale para os dois
    value = out_total if out_total is not None else in_total
    acquired = in_total if in_total is not None else value

    cost = ZERO
    cost_unknown = False
    single_out = len({_key(e) for e in outs}) == 1
    position_before = max(state.positions[_key(outs[0])].qty, ZERO) if single_out else None
    for leg in outs:
        position = state.positions[_key(leg)]
        covered = min(leg.qty, max(position.qty, ZERO))
        # parte do que está sendo vendido entrou sem custo comprovado: o custo médio está
        # subestimado até o usuário informar quanto pagou
        if not leg.stable and position.qty > 0 and position.unproven > _DUST:
            cost_unknown = True
        cost += position.remove(leg.qty)
        # stablecoin que entrou antes do histórico conhecido: o custo é o próprio valor.
        # Sem isto, vender USDC antigo apareceria como ganho de 100%.
        if covered >= leg.qty:
            continue
        if (leg.stable or unknown_cost == "market") and leg.brl_value is not None:
            cost += leg.brl_value * (leg.qty - covered) / leg.qty
        if not leg.stable:
            cost_unknown = True
    for leg in ins:
        leg_cost = (leg.brl_value or ZERO) if in_known else (acquired or ZERO) / len(ins)
        # entra o que chegou de fato; o custo é o que foi pago, inteiro
        state.positions[_key(leg)].add(_received(leg), leg_cost)

    first = outs[0]
    # o usuário informou o custo desta venda (compra fora do histórico lido): vale o informado
    cost_manual = first.cost_override is not None
    if first.cost_override is not None:
        cost, cost_unknown = first.cost_override, False
    ptax = next((e.ptax for e in (*outs, *ins) if e.ptax is not None), None)
    sold = _total_qty(outs)
    single_in = len({_key(e) for e in ins}) == 1
    return Row(
        id=first.id,
        ts=first.ts,
        type="swap",
        chain=first.chain,
        asset=f"{_label(outs)} → {_label(ins)}",
        quantity=_total_qty(outs),
        quantity_asset=first.symbol,
        tx_hash=_display_hash(outs),
        wallet_address=first.wallet_address,
        value=value,
        cost=cost if value is not None else ZERO,
        gain=(value - cost) if value is not None else ZERO,
        ptax=ptax,
        manual=any(e.policy == "manual" for e in (*outs, *ins)),
        slot=_slot(first),
        cost_unknown=cost_unknown,
        cost_manual=cost_manual,
        wallet_label=first.wallet_label,
        protocol=_protocol(first),
        fees=fees,
        # a fonte do preço que vale é a de quem definiu o valor da operação
        policy=(first if out_total is not None else ins[0]).policy,
        price_ts=first.price_ts,
        ptax_date=next((e.ptax_date for e in (*outs, *ins) if e.ptax_date is not None), None),
        fills=len(outs),
        quantity_in=sum((_received(leg) for leg in ins), ZERO) if single_in else None,
        quantity_in_asset=ins[0].symbol if single_in else None,
        position_before_qty=position_before,
        avg_cost_unit=cost / sold if single_out and value is not None and sold else None,
        unit_price=_exact_unit(outs) if out_total is not None else None,
    )


def _transfer_row(event: Event, direction: Literal["in", "out"]) -> Row:
    """Entrada (depósito ou recompensa) ou saída de cripto: não gera imposto, mas fica
    visível. Sem isso ela só mexia no custo de aquisição por trás, e o usuário não tinha
    como ver que a carteira foi lida (relatório de testes de 06/10, B9)."""
    other = event.raw.get("counterparty")
    return Row(
        direction=direction,
        counterparty=other if isinstance(other, str) else None,
        id=event.id,
        ts=event.ts,
        type="transfer",
        chain=event.chain,
        asset=event.symbol,
        quantity=event.qty,
        quantity_asset=event.symbol,
        tx_hash=event.tx_hash,
        wallet_address=event.wallet_address,
        value=event.brl_value,
        cost=event.brl_value or ZERO,
        gain=ZERO,
        ptax=event.ptax,
        manual=event.policy == "manual",
        reportable=False,
        wallet_label=event.wallet_label,
        protocol=_protocol(event),
        policy=event.policy,
        price_ts=event.price_ts,
        ptax_date=event.ptax_date,
        unit_price=_exact_unit([event]),
    )


def _perp_row(state: _State, event: Event) -> Row:
    closed = _decimal(event.raw.get("closedPnl"))
    state.perp_fees[event.asset] += _decimal(event.raw.get("fee"))
    row = Row(
        id=event.id,
        ts=event.ts,
        type="perp",
        chain=event.chain,
        asset=f"{event.asset}-PERP",
        quantity=event.qty,
        quantity_asset=event.asset,
        tx_hash=event.tx_hash,
        wallet_address=event.wallet_address,
        value=event.brl_value,
        ptax=event.ptax,
        manual=event.policy == "manual",
        wallet_label=event.wallet_label,
        protocol="Hyperliquid",
        fees=_decimal(event.raw.get("fee")) * event.ptax if event.ptax is not None else None,
        policy=event.policy,
        price_ts=event.price_ts,
        ptax_date=event.ptax_date,
        unit_price=_exact_unit([event]),
    )
    if closed == 0:
        # abertura ou aumento de posição: só acumula a taxa para o próximo fechamento
        row.reportable = False
        row.cost = event.brl_value or ZERO
        return row
    fees = state.perp_fees.pop(event.asset, ZERO)
    if event.ptax is not None and event.brl_value is not None:
        row.gain = (closed - fees) * event.ptax
        row.cost = event.brl_value - row.gain
    return row


def _funding_row(event: Event) -> Row:
    amount = _decimal(event.raw.get("usdc"))
    coin = str(event.raw.get("coin") or "")
    row = Row(
        id=event.id,
        ts=event.ts,
        type="funding",
        chain=event.chain,
        asset=f"{coin}-PERP" if coin else "Funding",
        quantity=event.qty,
        quantity_asset=event.symbol,
        tx_hash=event.tx_hash,
        wallet_address=event.wallet_address,
        ptax=event.ptax,
        manual=event.policy == "manual",
        wallet_label=event.wallet_label,
        protocol="Hyperliquid",
        fees=ZERO,
        policy=event.policy,
        price_ts=event.price_ts,
        ptax_date=event.ptax_date,
        # funding é pago em USDC: uma unidade vale a PTAX da linha, recebido ou pago
        unit_price=event.ptax,
    )
    if event.ptax is not None:
        gain = amount * event.ptax
        row.value, row.cost, row.gain = max(gain, ZERO), max(-gain, ZERO), gain
    return row


def compute_rows(events: list[Event], unknown_cost: UnknownCost = "zero") -> list[Row]:
    """Recebe todo o histórico do usuário em ordem cronológica e devolve as linhas tributáveis."""
    # transferência entre duas carteiras do próprio usuário: mesma transação, saída e entrada
    sides: dict[tuple[str, str], set[str]] = defaultdict(set)
    for event in events:
        if event.kind in ("transfer_in", "transfer_out"):
            sides[(event.tx_hash, event.asset)].add(event.kind)
    internal = {key for key, kinds in sides.items() if len(kinds) == 2}

    groups: dict[tuple[str, str], list[Event]] = {}
    for event in events:
        groups.setdefault((event.wallet_id, _order_key(event)), []).append(event)

    state = _State()
    rows: list[Row] = []
    for group in groups.values():
        outs = [e for e in group if e.kind == "swap_out"]
        ins = [e for e in group if e.kind == "swap_in"]
        if outs and ins:
            fee_events = [e for e in group if e.kind == "fee"]
            fees = (
                sum((e.brl_value or ZERO for e in fee_events), ZERO)
                if fee_events and all(e.brl_value is not None for e in fee_events)
                else _leg_fees_brl(outs)
            )
            rows.append(_swap_row(state, outs, ins, unknown_cost, fees))
        perp_rows: list[Row] = []
        for event in group:
            if event.kind in ("swap_in", "swap_out") and outs and ins:
                continue
            if event.kind in ("transfer_in", "transfer_out") and (event.tx_hash, event.asset) in internal:
                continue
            if event.kind in ("transfer_in", "reward"):
                if not _off_position(event):
                    # ponte da HyperEVM: o token já era do usuário e o custo não é conhecido
                    bridged = event.raw.get("bridge") is True
                    cost = event.brl_value or ZERO
                    if bridged and unknown_cost != "market":
                        cost = ZERO
                    state.positions[_key(event)].add(event.qty, cost, unproven=bridged)
                rows.append(_transfer_row(event, "in"))
            elif event.kind == "swap_in":
                state.positions[_key(event)].add(_received(event), event.brl_value or ZERO)
            elif event.kind == "transfer_out":
                if not _off_position(event):
                    state.positions[_key(event)].remove(event.qty)
                rows.append(_transfer_row(event, "out"))
            elif event.kind in ("fee", "swap_out"):
                if not _off_position(event):
                    state.positions[_key(event)].remove(event.qty)
            elif event.kind == "perp_fill":
                perp_rows.append(_perp_row(state, event))
            elif event.kind == "funding":
                rows.append(_funding_row(event))
            # stake, unstake e other: o ativo continua do usuário, a posição não muda
        rows.extend(_merge_perp_rows(perp_rows))
    for row in rows:
        _round_row(row)
    return rows


def summarize(rows: list[Row], exemption_limit: Decimal, rate: Decimal) -> Totals:
    totals = Totals()
    spot_gain = derivative_gain = ZERO
    for row in rows:
        if not row.reportable:
            continue
        if not row.priced:
            totals.missing_prices += 1
            continue
        totals.rows += 1
        totals.disposed += row.value or ZERO
        totals.cost += row.cost
        totals.gain += row.gain
        if row.type == "swap":
            totals.disposals += 1
            totals.spot_disposed += row.value or ZERO
            spot_gain += row.gain
        else:
            derivative_gain += row.gain
    taxable = derivative_gain + (spot_gain if totals.spot_disposed > exemption_limit else ZERO)
    totals.tax = max(taxable, ZERO) * rate
    return totals
