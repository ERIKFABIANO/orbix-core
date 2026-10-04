from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from orbix.config import Settings
from orbix.db import Database
from orbix.deps import CurrentUser, current_user, get_db, settings_dep
from orbix.errors import AppError, not_found
from orbix.schemas import MONTH_RE, DashboardOut, ManualCostIn, ManualPriceIn, TaxEventOut
from orbix.tax import service as tax
from orbix.tax.engine import money

router = APIRouter(prefix="/api", tags=["dashboard"])


def valid_month(month: str) -> str:
    if not MONTH_RE.fullmatch(month):
        raise AppError("invalid_month", 422)
    return month


def _previous(month: str) -> str:
    year, number = int(month[:4]), int(month[5:])
    return f"{year - 1}-12" if number == 1 else f"{year}-{number - 1:02d}"


@router.get("/dashboard", response_model=DashboardOut)
async def dashboard(
    month: str | None = Query(default=None, max_length=7),
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> DashboardOut:
    async with db.as_user(user.id) as conn:
        rows = await tax.load_rows(conn)
        updated_at = await conn.fetchval("select max(last_synced_at) from public.wallets")
    if month is None:
        # sem mês informado: o mais recente com dados
        available = tax.months_with_rows(rows)
        month = available[0] if available else tax.current_month()
    else:
        valid_month(month)

    current = tax.totals_of(tax.rows_of(rows, month), settings)
    previous = tax.totals_of(tax.rows_of(rows, _previous(month)), settings)
    change = (
        float(((current.gain - previous.gain) / abs(previous.gain) * 100).quantize(Decimal("0.1")))
        if previous.gain != 0
        else None
    )
    return DashboardOut(
        month=month,
        updated_at=updated_at or datetime.now(UTC),
        volume_brl=money(current.disposed),
        disposals=current.disposals,
        capital_gain_brl=money(current.gain),
        gain_change_pct=change,
        estimated_tax_brl=money(current.tax),
        exemption_limit_brl=float(settings.exemption_limit_brl),
        exemption_status=tax.exemption_status(current, settings),
        missing_prices=current.missing_prices,
    )


@router.get("/events", response_model=list[TaxEventOut])
async def events(
    month: str = Query(max_length=7),
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
) -> list[TaxEventOut]:
    valid_month(month)
    async with db.as_user(user.id) as conn:
        rows = await tax.load_rows(conn)
        selected = sorted(tax.rows_of(rows, month), key=lambda r: r.ts, reverse=True)
        history = await tax.load_review_history(conn, [row.id for row in selected])
    return [tax.to_tax_event(row, history.get(row.id)) for row in selected]


async def _attach_evidence(db: Database, event_id: UUID, user_id: UUID, evidence: str) -> None:
    """A linha de auditoria acabou de ser criada pelo trigger do banco; a evidência entra nela."""
    async with db.service() as conn:
        await conn.execute(
            """
            update public.event_reviews set evidence = jsonb_build_object('text', $3::text)
             where id = (select id from public.event_reviews
                          where event_id = $1 and actor_user_id = $2
                          order by created_at desc limit 1)
            """,
            event_id,
            user_id,
            evidence,
        )


async def _event_out(db: Database, user: CurrentUser, event_id: str) -> TaxEventOut:
    async with db.as_user(user.id) as conn:
        rows = await tax.load_rows(conn)
        history = await tax.load_review_history(conn, [event_id])
    row = next((r for r in rows if r.id == event_id), None)
    if row is None:
        raise not_found()
    return tax.to_tax_event(row, history.get(event_id))


def _event_uuid(event_id: str) -> UUID:
    try:
        return UUID(event_id)
    except ValueError:
        raise not_found() from None


@router.put("/events/{event_id}/price", response_model=TaxEventOut)
async def set_manual_price(
    event_id: str,
    body: ManualPriceIn,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
) -> TaxEventOut:
    target = _event_uuid(event_id)
    # revisão auditável: motivo, evidência e confirmação andam juntos
    audited = body.reason is not None or body.evidence is not None or body.confirmed is not None
    if audited and not (body.reason and body.evidence and body.confirmed):
        raise AppError("review_incomplete", 422)
    reason = body.reason or "preço informado pelo usuário"
    unit = Decimal(str(body.unit_price_brl))
    async with db.as_user(user.id) as conn:
        # Um único UPDATE: o trigger de auditoria grava uma linha em event_reviews por alteração.
        # O RLS garante que só o dono altera. Sem PTAX gravada, usa a do dia do evento.
        updated = await conn.fetchval(
            """
            update public.events e
               set brl_value = e.qty * $2, pricing_policy = 'manual', price_ts = now(),
                   review_status = 'reviewed', review_reason = $3,
                   ptax = coalesce(e.ptax, (
                     select f.ptax_sell from public.fx_rates f
                      where f.date <= (e.ts at time zone 'America/Sao_Paulo')::date
                      order by f.date desc limit 1)),
                   ptax_date = coalesce(e.ptax_date, (
                     select f.date from public.fx_rates f
                      where f.date <= (e.ts at time zone 'America/Sao_Paulo')::date
                      order by f.date desc limit 1))
             where e.id = $1
            returning e.id
            """,
            target,
            unit,
            reason,
        )
    if updated is None:
        raise not_found()
    if body.evidence:
        await _attach_evidence(db, target, user.id, body.evidence)
    return await _event_out(db, user, event_id)


@router.put("/events/{event_id}/cost", response_model=TaxEventOut)
async def set_acquisition_cost(
    event_id: str,
    body: ManualCostIn,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
) -> TaxEventOut:
    """Custo de aquisição informado para uma venda cuja compra não está no histórico lido.

    Exige motivo, evidência e confirmação: fica registrado em event_reviews.
    """
    target = _event_uuid(event_id)
    async with db.as_user(user.id) as conn:
        # só a saída de um swap identifica a venda; o RLS garante que só o dono altera
        updated = await conn.fetchval(
            "update public.events set cost_override_brl = $2, review_status = 'reviewed', "
            "review_reason = $3 where id = $1 and kind = 'swap_out' returning id",
            target,
            Decimal(str(body.cost_brl)),
            body.reason,
        )
    if updated is None:
        raise not_found()
    await _attach_evidence(db, target, user.id, body.evidence)
    return await _event_out(db, user, event_id)
