import base64
import secrets
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import asyncpg
from fastapi import APIRouter, Depends, Request

from orbix.config import Settings
from orbix.db import Database
from orbix.deps import CurrentUser, Queue, current_user, get_db, get_queue, settings_dep
from orbix.errors import AppError, not_found
from orbix.queue import attest_job_id
from orbix.ratelimit import limiter
from orbix.reports.files import build_csv, build_decripto
from orbix.routers.dashboard import valid_month
from orbix.schemas import (
    AttestationOut,
    DownloadLinkOut,
    ReportDetailOut,
    ReportReviewItemOut,
    ReportReviewOut,
    ReportSummaryOut,
    ReportTotalsOut,
)
from orbix.security import sha256_hex
from orbix.storage import Storage, user_prefix
from orbix.tax import service as tax
from orbix.tax.engine import Totals, money

router = APIRouter(prefix="/api", tags=["reports"])

# colunas que o usuário pode ler em reports (sem `salt`)
FINAL_COLUMNS = (
    "id, month, status, total_brl, events_count, data, file_url, decripto_key, sha256, "
    "solana_sig, slot, attested_at, public_id, finalized_at, updated_at"
)


_month_date = tax.month_date


def _month_key(value: date) -> str:
    return value.strftime("%Y-%m")


async def _final(conn: asyncpg.Connection, month: str) -> asyncpg.Record | None:
    return await conn.fetchrow(
        f"select {FINAL_COLUMNS} from public.reports where month = $1 and status = 'final'",  # noqa: S608  # nosec B608
        _month_date(month),
    )


def _attestation(row: asyncpg.Record) -> AttestationOut | None:
    if not (row["solana_sig"] and row["slot"] is not None and row["attested_at"]):
        return None
    return AttestationOut(
        hash=row["sha256"],
        tx_signature=row["solana_sig"],
        slot=row["slot"],
        registered_at=row["attested_at"],
        public_id=row["public_id"],
    )


@router.get("/reports", response_model=list[ReportSummaryOut])
async def list_reports(
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> list[ReportSummaryOut]:
    async with db.as_user(user.id) as conn:
        finals = await conn.fetch(
            "select month, total_brl, events_count, finalized_at, updated_at "
            "from public.reports where status = 'final'"
        )
        rows = await tax.load_rows(conn)

    summaries = {
        _month_key(r["month"]): ReportSummaryOut(
            month=_month_key(r["month"]),
            status="final",
            events=r["events_count"],
            total_brl=float(round(r["total_brl"], 2)),
            updated_at=r["finalized_at"] or r["updated_at"],
        )
        for r in finals
    }
    now = datetime.now(UTC)
    for month in tax.months_with_rows(rows):
        if month in summaries:
            continue
        month_rows = tax.rows_of(rows, month)
        totals = tax.totals_of(month_rows, settings)
        summaries[month] = ReportSummaryOut(
            month=month,
            status="draft",
            events=totals.rows + totals.missing_prices,
            total_brl=money(totals.disposed),
            updated_at=max((r.ts for r in month_rows), default=now),
        )
    return [summaries[month] for month in sorted(summaries, reverse=True)]


@router.get("/report/{month}", response_model=ReportDetailOut)
async def report_detail(
    month: str,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    queue: Queue = Depends(get_queue),
    settings: Settings = Depends(settings_dep),
) -> ReportDetailOut:
    valid_month(month)
    return await _detail(user, db, queue, settings, month)


async def _detail(
    user: CurrentUser, db: Database, queue: Queue, settings: Settings, month: str
) -> ReportDetailOut:
    async with db.as_user(user.id) as conn:
        final = await _final(conn, month)
        coverage, run_limitations = await tax.load_coverage(conn)
        rows = [] if final is not None else await tax.load_rows(conn)
    if final is not None:
        if not final["solana_sig"]:
            # a gravação na Solana ainda não aconteceu (rede fora, carteira sem saldo):
            # tentar de novo. O job_id impede tentativas em paralelo.
            await queue.enqueue("attest_report", str(final["id"]), job_id=attest_job_id(final["id"]))
        # relatório final é o que foi congelado; não é recalculado
        data = cast(dict[str, Any], final["data"])
        review = tax.build_review([], Totals(), coverage, run_limitations, settings)
        return ReportDetailOut(
            month=month,
            status="final",
            totals=ReportTotalsOut(**data["totals"]),
            rows=data["rows"],
            attestation=_attestation(final),
            review=_with_frozen_items(review, data["rows"]),
        )
    month_rows = tax.rows_of(rows, month)
    totals = tax.totals_of(month_rows, settings)
    return ReportDetailOut(
        month=month,
        status="draft",
        totals=tax.to_totals(totals),
        rows=[tax.to_report_row(row) for row in tax.report_rows(month_rows)],
        attestation=None,
        review=tax.build_review(month_rows, totals, coverage, run_limitations, settings),
    )


def _with_frozen_items(review: ReportReviewOut, frozen_rows: list[dict[str, Any]]) -> ReportReviewOut:
    """Itens a revisar de um relatório já congelado: linhas sem custo de aquisição comprovado."""
    items = [
        ReportReviewItemOut(id=str(row["id"]), kind="acquisition_cost", label=str(row["asset"]))
        for row in frozen_rows
        if row.get("cost_unknown")
    ]
    return review.model_copy(
        update={"review_items": items, "decripto_ready": review.decripto_ready and not items}
    )


@router.get("/report/{month}/csv", response_model=DownloadLinkOut)
@limiter.limit("30/minute")
async def report_csv(
    request: Request,
    month: str,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> DownloadLinkOut:
    valid_month(month)
    async with db.as_user(user.id) as conn:
        final = await _final(conn, month)
        rows = [] if final is not None else await tax.load_rows(conn)
    if final is not None:
        storage = _storage(request)
        url, expires_at = await storage.presign(final["file_url"], f"orbix-declare-{month}.csv")
        return DownloadLinkOut(url=url, filename=f"orbix-declare-{month}.csv", expires_at=expires_at)

    # rascunho: o arquivo vai embutido na resposta e nada é guardado no servidor
    month_rows = tax.rows_of(rows, month)
    content = build_csv(tax.report_rows(month_rows), tax.totals_of(month_rows, settings), nonce=None)
    return DownloadLinkOut(
        url="data:text/csv;charset=utf-8;base64," + base64.b64encode(content).decode(),
        filename=f"orbix-declare-{month}-rascunho.csv",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


def _storage(request: Request) -> Storage:
    storage = cast(Storage | None, request.app.state.storage)
    if storage is None:
        raise AppError("storage_unavailable", 503)
    return storage


@router.post("/report/{month}/decripto", response_model=DownloadLinkOut)
@limiter.limit("10/minute")
async def generate_decripto(
    request: Request,
    month: str,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    queue: Queue = Depends(get_queue),
    settings: Settings = Depends(settings_dep),
) -> DownloadLinkOut:
    """Devolve o arquivo da DeCripto, finalizando o relatório antes se ele ainda for rascunho."""
    valid_month(month)
    storage = _storage(request)
    async with db.as_user(user.id) as conn:
        final = await _final(conn, month)
    if final is None:
        await _finalize(storage, user, db, queue, settings, month)
        async with db.as_user(user.id) as conn:
            final = await _final(conn, month)
    if final is None:
        raise not_found()
    filename = f"decripto-{month}.txt"
    url, expires_at = await storage.presign(final["decripto_key"], filename)
    return DownloadLinkOut(url=url, filename=filename, expires_at=expires_at)


@router.post("/report/{month}/finalize", response_model=ReportDetailOut)
@limiter.limit("10/minute")
async def finalize_report(
    request: Request,
    month: str,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    queue: Queue = Depends(get_queue),
    settings: Settings = Depends(settings_dep),
) -> ReportDetailOut:
    """Congela o relatório do mês: CSV imutável no R2, hash e gravação na Solana.

    Chamar de novo num relatório já final só devolve o relatório, sem alterar nada.
    """
    valid_month(month)
    storage = _storage(request)
    async with db.as_user(user.id) as conn:
        final = await _final(conn, month)
    if final is None:
        await _finalize(storage, user, db, queue, settings, month)
    return await _detail(user, db, queue, settings, month)


async def _finalize(
    storage: Storage, user: CurrentUser, db: Database, queue: Queue, settings: Settings, month: str
) -> None:
    filename = f"decripto-{month}.txt"
    async with db.as_user(user.id) as conn:
        rows = await tax.load_rows(conn)

    if not tax.is_closed(month):
        raise AppError("month_open", 409)
    month_rows = tax.rows_of(rows, month)
    totals = tax.totals_of(month_rows, settings)
    if totals.missing_prices:
        raise AppError("missing_prices", 409, count=totals.missing_prices)
    reportable = tax.report_rows(month_rows)
    if not reportable:
        raise AppError("nothing_to_report", 409)

    nonce = secrets.token_hex(32)
    csv_bytes = build_csv(reportable, totals, nonce=nonce)
    report_hash = sha256_hex(csv_bytes)
    prefix = f"{user_prefix(user.id)}{month}/"
    csv_key, decripto_key = f"{prefix}orbix-declare-{month}.csv", f"{prefix}{filename}"

    async with db.service() as conn:
        public_id = await _free_public_id(conn, report_hash)
    decripto_bytes = build_decripto(
        month,
        reportable,
        totals,
        report_hash=report_hash,
        verify_url=f"{settings.app_url}/v/{public_id}",
        limit=settings.exemption_limit_brl,
    )
    await storage.put(csv_key, csv_bytes, "text/csv; charset=utf-8")
    await storage.put(decripto_key, decripto_bytes, "text/plain; charset=utf-8")

    data = {
        "totals": tax.to_totals(totals).model_dump(),
        "rows": [tax.to_report_row(row).model_dump(mode="json") for row in reportable],
    }
    async with db.service() as conn:
        report_id = await conn.fetchval(
            """
            insert into public.reports
              (user_id, month, status, total_brl, gains_brl, cost_brl, tax_brl, over_35k, data,
               file_url, decripto_key, sha256, salt, public_id, events_count, finalized_at,
               r2_object_key, reporting_required, rules_version)
            values ($1, $2, 'final', $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, now(),
                    $9, $7, $15)
            on conflict (user_id, month) do update set
              status = 'final', total_brl = excluded.total_brl, gains_brl = excluded.gains_brl,
              cost_brl = excluded.cost_brl, tax_brl = excluded.tax_brl, over_35k = excluded.over_35k,
              data = excluded.data, file_url = excluded.file_url,
              decripto_key = excluded.decripto_key, sha256 = excluded.sha256, salt = excluded.salt,
              public_id = excluded.public_id, events_count = excluded.events_count,
              r2_object_key = excluded.r2_object_key,
              reporting_required = excluded.reporting_required,
              rules_version = excluded.rules_version, finalized_at = now()
            where public.reports.status <> 'final'
            returning id
            """,
            user.id,
            _month_date(month),
            totals.disposed,
            totals.gain,
            totals.cost,
            totals.tax,
            totals.spot_disposed > settings.exemption_limit_brl,
            data,
            csv_key,
            decripto_key,
            report_hash,
            bytes.fromhex(nonce),
            public_id,
            len(reportable),
            tax.RULES_VERSION,
        )
    if report_id is not None:
        await queue.enqueue("attest_report", str(report_id), job_id=attest_job_id(report_id))


async def _free_public_id(conn: asyncpg.Connection, report_hash: str) -> str:
    # 8 caracteres do hash; em caso de colisão, usa mais
    for size in range(8, 65, 2):
        candidate = report_hash[:size]
        taken = await conn.fetchval(
            "select exists(select 1 from public.reports where public_id = $1)", candidate
        )
        if not taken:
            return candidate
    raise not_found()
