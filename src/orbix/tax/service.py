"""Carrega os eventos do usuário (via RLS) e monta as visões que as rotas devolvem."""

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

import asyncpg

from orbix.config import Settings, get_settings
from orbix.i18n import tr
from orbix.ingest.models import KNOWN_SYMBOLS, is_stable, position_token, short
from orbix.schemas import (
    CoverageOut,
    EventReviewOut,
    Network,
    ReportReviewItemOut,
    ReportReviewOut,
    ReportRowOut,
    ReportTotalsOut,
    TaxEventOut,
    WalletRefOut,
)
from orbix.tax.engine import BRT, Event, Row, Totals, compute_rows, money, summarize

SOLANA_EXPLORER = "https://explorer.solana.com/tx/"
HYPERLIQUID_EXPLORER = "https://app.hyperliquid.xyz/explorer"

# versão das regras de cálculo (ver tax/engine.py); muda quando uma regra mudar
RULES_VERSION = "br-2026.1"
# o gerador da DeCripto ainda não segue o leiaute oficial de transmissão da Receita
DECRIPTO_OFFICIAL_LAYOUT = False

PRICE_PROVIDERS = {
    "coingecko": "CoinGecko",
    "counter_leg": "Outra perna do swap",
    "stable": "Stablecoin (US$ 1)",
    "hyperliquid_fill": "Hyperliquid",
    "hyperliquid_ledger": "Hyperliquid",
}
# motivos das revisões automáticas: não aparecem no histórico do usuário
AUTOMATIC_REASONS = ("cotação automática", "server update; reason not supplied")


async def load_rows(conn: asyncpg.Connection) -> list[Row]:
    """`conn` precisa estar em `as_user`: o RLS entrega só os eventos do próprio usuário."""
    records = await conn.fetch(
        """
        select e.id, e.wallet_id, e.chain, e.tx_hash, e.ts, e.kind, e.asset, e.qty, e.brl_value,
               e.ptax, e.ptax_date, e.price_ts, e.pricing_policy, e.raw, e.cost_override_brl,
               e.usd_price,
               a.symbol, a.is_stable,
               w.address, w.label
          from public.events e
          join public.wallets w on w.id = e.wallet_id
          left join public.assets a on a.chain = e.chain and a.asset = e.asset
         order by e.ts, e.tx_hash, e.event_index
        """
    )
    events = [
        Event(
            id=str(r["id"]),
            wallet_id=str(r["wallet_id"]),
            chain=r["chain"],
            tx_hash=r["tx_hash"],
            ts=r["ts"],
            kind=r["kind"],
            asset=r["asset"],
            qty=r["qty"],
            brl_value=r["brl_value"],
            ptax=r["ptax"],
            policy=r["pricing_policy"],
            raw=r["raw"] or {},
            symbol=r["symbol"] or KNOWN_SYMBOLS.get(r["asset"]) or short(r["asset"]),
            wallet_address=r["address"],
            stable=bool(r["is_stable"]) or is_stable(r["chain"], r["asset"], r["symbol"]),
            wallet_label=r["label"],
            price_ts=r["price_ts"],
            ptax_date=r["ptax_date"],
            cost_override=r["cost_override_brl"],
            usd_price=r["usd_price"],
            position_token=position_token(r["chain"], r["asset"], r["symbol"]),
        )
        for r in records
    ]
    return compute_rows(events, get_settings().unknown_cost_policy)


def rows_of(rows: list[Row], month: str) -> list[Row]:
    return [row for row in rows if row.month == month]


def months_with_rows(rows: list[Row]) -> list[str]:
    return sorted({row.month for row in rows if row.reportable}, reverse=True)


def totals_of(rows: list[Row], settings: Settings) -> Totals:
    return summarize(rows, settings.exemption_limit_brl, settings.capital_gain_rate)


def exemption_status(totals: Totals, settings: Settings) -> str | None:
    """Isento ou tributável no mês. None enquanto houver evento sem preço: o total pode mudar."""
    if totals.missing_prices:
        return None
    return "exempt" if totals.spot_disposed <= settings.exemption_limit_brl else "taxable"


def current_month() -> str:
    return datetime.now(BRT).strftime("%Y-%m")


def is_closed(month: str) -> bool:
    return month < current_month()


def explorer_url(row: Row) -> str:
    if row.chain == "solana":
        return f"{SOLANA_EXPLORER}{row.tx_hash}"
    if row.tx_hash.startswith("0x") and set(row.tx_hash[2:]) != {"0"}:
        return f"{HYPERLIQUID_EXPLORER}/tx/{row.tx_hash}"
    return f"{HYPERLIQUID_EXPLORER}/address/{row.wallet_address}"


def pending_reasons(row: Row) -> list[str]:
    reasons: list[str] = []
    if row.reportable and not row.priced:
        reasons.append(
            tr(
                "Nenhuma fonte de preço foi encontrada para este ativo. Informe o preço em reais.",
                "No price source was found for this asset. Enter the price in reais.",
            )
        )
    if row.cost_unknown:
        reasons.append(
            tr(
                "A compra deste ativo não está no histórico lido; o custo de aquisição foi considerado zero.",
                "The purchase of this asset is not in the history read; "
                "the acquisition cost was taken as zero.",
            )
        )
    return reasons


def _money_or_none(value: Decimal | None) -> float | None:
    return money(value) if value is not None else None


def to_tax_event(row: Row, history: list[EventReviewOut] | None = None) -> TaxEventOut:
    unit = row.unit_price
    return TaxEventOut(
        id=row.id,
        date=row.ts,
        network=cast(Network, row.chain),
        type=row.type,
        asset=row.asset,
        quantity=float(row.quantity),
        quantity_asset=row.quantity_asset,
        value_brl=money(row.value) if row.priced else None,
        price_source=("manual" if row.manual else "auto") if row.priced else None,
        tx_hash=row.tx_hash,
        explorer_url=explorer_url(row),
        wallet=WalletRefOut(address=row.wallet_address, label=row.wallet_label),
        protocol=row.protocol,
        unit_price_brl=float(unit.quantize(Decimal("0.00000001"))) if unit is not None else None,
        price_provider=None if row.manual or not row.priced else PRICE_PROVIDERS.get(row.policy or ""),
        ptax=float(row.ptax) if row.ptax is not None else None,
        ptax_date=row.ptax_date,
        price_observed_at=row.price_ts,
        rule_version=RULES_VERSION,
        fees_brl=_money_or_none(row.fees),
        cost_brl=money(row.cost) if row.priced and row.reportable else None,
        gain_brl=money(row.gain) if row.priced and row.reportable else None,
        fill_count=row.fills,
        cost_unknown=row.cost_unknown,
        cost_manual=row.cost_manual,
        direction=row.direction,
        counterparty=row.counterparty,
        quantity_in=float(row.quantity_in) if row.quantity_in is not None else None,
        quantity_in_asset=row.quantity_in_asset,
        position_before_qty=float(row.position_before_qty) if row.position_before_qty is not None else None,
        avg_cost_unit_brl=float(row.avg_cost_unit.quantize(Decimal("0.00000001")))
        if row.avg_cost_unit is not None
        else None,
        pending_reasons=pending_reasons(row),
        review_history=history or [],
    )


def _unit_price(state: Any) -> float | None:
    if not isinstance(state, dict):
        return None
    try:
        value, qty = state.get("brl_value"), Decimal(str(state.get("qty")))
        return money(Decimal(str(value)) / qty) if value is not None and qty else None
    except (ArithmeticError, ValueError):
        return None


def _iso_day(value: Any) -> str:
    """ "2026-10-06" -> "06/10/2026"; devolve o texto como veio se não for uma data."""
    try:
        return date.fromisoformat(str(value)).strftime("%d/%m/%Y")
    except ValueError:
        return str(value)


def _cost(state: Any) -> float | None:
    value = state.get("cost_override_brl") if isinstance(state, dict) else None
    try:
        return money(Decimal(str(value))) if value is not None else None
    except (ArithmeticError, ValueError):
        return None


async def load_review_history(
    conn: asyncpg.Connection, event_ids: list[str]
) -> dict[str, list[EventReviewOut]]:
    """Revisões feitas pelo usuário (as automáticas ficam de fora). `conn` em `as_user`."""
    if not event_ids:
        return {}
    records = await conn.fetch(
        """
        select event_id, reason, evidence, before_state, after_state, created_at
          from public.event_reviews
         where event_id = any($1::uuid[])
           and ((actor_user_id is not null and not (reason = any($2::text[])))
                -- recotação: o evento já tinha PTAX e passou a usar a de outro dia (B15)
                or (reason = $3
                    and before_state->>'ptax_date' is not null
                    and before_state->>'ptax_date' is distinct from after_state->>'ptax_date'))
         order by created_at
        """,
        event_ids,
        list(AUTOMATIC_REASONS),
        AUTOMATIC_REASONS[0],
    )
    history: dict[str, list[EventReviewOut]] = defaultdict(list)
    for r in records:
        before, after = r["before_state"], r["after_state"]
        if r["reason"] == AUTOMATIC_REASONS[0]:
            new_price = _unit_price(after)
            if new_price is None:
                continue
            old_day, new_day = _iso_day(before.get("ptax_date")), _iso_day(after.get("ptax_date"))
            history[str(r["event_id"])].append(
                EventReviewOut(
                    kind="price",
                    reason=tr(
                        f"Correção automática do câmbio: a PTAX de {old_day} foi trocada pela de "
                        f"{new_day}, que ainda não tinha saído quando o evento foi lido.",
                        f"Automatic exchange-rate correction: the PTAX of {old_day} was replaced by the "
                        f"one of {new_day}, which had not been published when the event was read.",
                    ),
                    evidence=tr(
                        f"PTAX de venda {before.get('ptax')} → {after.get('ptax')} (Banco Central)",
                        f"PTAX selling rate {before.get('ptax')} → {after.get('ptax')} (Central Bank)",
                    ),
                    previous_price_brl=_unit_price(before),
                    new_price_brl=new_price,
                    created_at=r["created_at"],
                )
            )
            continue
        evidence = r["evidence"] if isinstance(r["evidence"], dict) else {}
        new_cost, old_cost = _cost(after), _cost(before)
        is_cost = new_cost is not None and new_cost != old_cost
        new_price = _unit_price(after)
        if new_price is None and not is_cost:
            continue
        history[str(r["event_id"])].append(
            EventReviewOut(
                kind="cost" if is_cost else "price",
                reason=r["reason"],
                evidence=str(evidence.get("text") or ""),
                previous_price_brl=_unit_price(before),
                new_price_brl=new_price if new_price is not None else 0.0,
                previous_cost_brl=old_cost if is_cost else None,
                new_cost_brl=new_cost if is_cost else None,
                created_at=r["created_at"],
            )
        )
    return history


def to_report_row(row: Row) -> ReportRowOut:
    return ReportRowOut(
        id=row.id,
        date=row.ts,
        type=row.type,
        asset=row.asset,
        quantity=float(row.quantity),
        ptax=float((row.ptax or Decimal(0)).quantize(Decimal("0.0001"))),
        value_brl=money(row.value),
        cost_brl=money(row.cost),
        gain_brl=money(row.gain),
        manual_price=row.manual,
        cost_unknown=row.cost_unknown,
        cost_manual=row.cost_manual,
    )


def report_rows(rows: list[Row]) -> list[Row]:
    """Linhas que entram no relatório: tributáveis e com preço."""
    return [row for row in rows if row.reportable and row.priced]


def to_totals(totals: Totals) -> ReportTotalsOut:
    return ReportTotalsOut(
        disposed_brl=money(totals.disposed),
        cost_brl=money(totals.cost),
        gain_brl=money(totals.gain),
        tax_brl=money(totals.tax),
    )


# ── cobertura da leitura e revisão do relatório ───────────────────────────


async def load_coverage(conn: asyncpg.Connection) -> tuple[CoverageOut, list[str]]:
    """Cobertura do histórico lido e as limitações registradas. `conn` em `as_user`."""
    wallets = await conn.fetch("select id, status, last_synced_at from public.wallets")
    runs = await conn.fetch(
        """
        select distinct on (wallet_id) wallet_id, status, limitations
          from public.ingestion_runs
         order by wallet_id, started_at desc
        """
    )
    span = await conn.fetchrow("select min(ts) as first, count(*) as total from public.events")
    if not wallets or not any(w["last_synced_at"] for w in wallets):
        return CoverageOut(
            state="unknown", imported_from=None, imported_through=None, imported_events=None
        ), []

    latest = {r["wallet_id"]: r for r in runs}
    limitations = sorted({text for r in runs if (text := _run_limitation(r["limitations"]))})
    incomplete = any(w["status"] in ("error", "pending", "syncing") for w in wallets) or any(
        latest.get(w["id"]) is not None and latest[w["id"]]["status"] in ("partial", "failed", "running")
        for w in wallets
    )
    through = max(w["last_synced_at"] for w in wallets if w["last_synced_at"])
    first: datetime | None = span["first"] if span else None
    return (
        CoverageOut(
            state="partial" if incomplete else "complete",
            imported_from=first.astimezone(BRT).date() if first else None,
            imported_through=through.astimezone(BRT).date(),
            imported_events=int(span["total"]) if span else 0,
        ),
        limitations,
    )


def _run_limitation(code: str | None) -> str | None:
    """Traduz o código gravado em ingestion_runs.limitations para o idioma da resposta."""
    if not code:
        return None
    if code.startswith("truncated:"):
        limit = code.split(":", 1)[1]
        return tr(
            f"A leitura de uma carteira parou nas {limit} transações mais recentes; "
            "operações mais antigas não entram no cálculo.",
            f"Reading a wallet stopped at its {limit} most recent transactions; "
            "older operations are not included in the calculation.",
        )
    if code.startswith("failed:"):
        return tr(
            "A última leitura de uma carteira falhou; os dados dela podem estar desatualizados.",
            "The last read of a wallet failed; its data may be out of date.",
        )
    return None


def _day(moment: datetime) -> str:
    return moment.astimezone(BRT).strftime("%d/%m/%Y")


def build_review(
    month_rows: list[Row],
    totals: Totals,
    coverage: CoverageOut,
    run_limitations: list[str],
    settings: Settings,
) -> ReportReviewOut:
    limitations = [
        tr(
            "O arquivo DeCripto gerado é um resumo de apoio; "
            "ainda não segue o leiaute oficial de transmissão da Receita.",
            "The generated DeCripto file is a supporting summary; "
            "it does not follow the Receita's official filing layout yet.",
        ),
        tr(
            "Imposto estimado com alíquota única de 15%; faixas progressivas não são aplicadas.",
            "Tax estimated at a flat 15% rate; progressive brackets are not applied.",
        ),
        tr(
            "Os valores são estimativas e não substituem a orientação de um contador.",
            "Amounts are estimates and do not replace advice from an accountant.",
        ),
        *run_limitations,
    ]
    if settings.solana_memo_cluster != "mainnet-beta":
        limitations.append(
            tr(
                "O registro do hash na Solana é feito na rede de testes (devnet).",
                "The hash is recorded on the Solana test network (devnet).",
            )
        )

    pending: list[str] = []
    if totals.missing_prices:
        pending.append(
            tr(
                f"{totals.missing_prices} evento(s) sem preço neste mês.",
                f"{totals.missing_prices} event(s) without a price this month.",
            )
        )
    if coverage.state != "complete":
        pending.append(
            tr(
                "A leitura das carteiras não está completa.",
                "Reading the wallets is not complete.",
            )
        )

    items = [
        ReportReviewItemOut(
            id=row.id,
            kind="acquisition_cost",
            label=tr(
                f"{row.asset} em {_day(row.ts)}: compra de {row.quantity_asset} "
                "não encontrada no histórico lido",
                f"{row.asset} on {_day(row.ts)}: purchase of {row.quantity_asset} "
                "not found in the history read",
            ),
        )
        for row in month_rows
        if row.reportable and row.cost_unknown
    ]
    return ReportReviewOut(
        engine_version=RULES_VERSION,
        coverage=coverage,
        limitations=limitations,
        pending_reasons=pending,
        unsupported_operations=[],
        review_items=items,
        decripto_ready=DECRIPTO_OFFICIAL_LAYOUT
        and not pending
        and not items
        and coverage.state == "complete",
    )


def month_date(month: str) -> date:
    return date(int(month[:4]), int(month[5:]), 1)
