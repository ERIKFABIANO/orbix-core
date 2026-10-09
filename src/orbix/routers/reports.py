import base64
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Request

from orbix.config import Settings
from orbix.db import Database
from orbix.deps import CurrentUser, Queue, current_user, get_db, get_queue, settings_dep
from orbix.errors import AppError, not_found
from orbix.i18n import tr
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
    ReportVersionOut,
)
from orbix.security import sha256_hex
from orbix.storage import Storage, user_prefix
from orbix.tax import service as tax
from orbix.tax.engine import Row, Totals, money

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


# ── versões do relatório final ────────────────────────────────────────────
#
# A primeira finalização grava "orbix-declare-AAAA-MM.csv". Cada nova versão grava um arquivo
# próprio ("…-v2.csv", "…-v3.csv"): o arquivo antigo não é sobrescrito, porque o hash dele já
# está numa transação e o link público dele continua valendo.

_VERSION = re.compile(r"-v(\d+)\.[a-z]+$")


def _version(file_key: str | None) -> int:
    match = _VERSION.search(file_key or "")
    return int(match.group(1)) if match else 1


def _suffix(version: int) -> str:
    return "" if version <= 1 else f"-v{version}"


def _basename(key: str) -> str:
    return key.rsplit("/", 1)[-1]


def _current_data(reportable: list[Row], totals: Totals) -> dict[str, Any]:
    """O que fica congelado em reports.data: totais e linhas, no formato da resposta."""
    return {
        "totals": tax.to_totals(totals).model_dump(),
        "rows": [tax.to_report_row(row).model_dump(mode="json") for row in reportable],
    }


def _cents(value: Any) -> float:
    try:
        return round(float(value or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def _signature(data: dict[str, Any]) -> tuple[Any, ...]:
    """Os números de um relatório, para saber se o congelado ainda bate com o cálculo de hoje.

    Compara os totais e, linha a linha, ativo, valor, custo e ganho. A ordem das linhas e os
    campos que entraram depois (custo informado, por exemplo) ficam de fora: um relatório
    antigo não vira "desatualizado" só porque a resposta ganhou um campo."""
    totals = data.get("totals") or {}
    rows = sorted(
        (
            str(r.get("asset")),
            _cents(r.get("value_brl")),
            _cents(r.get("cost_brl")),
            _cents(r.get("gain_brl")),
        )
        for r in data.get("rows") or []
        if isinstance(r, dict)
    )
    return (
        tuple(_cents(totals.get(key)) for key in ("disposed_brl", "cost_brl", "gain_brl", "tax_brl")),
        tuple(rows),
    )


def _brl(value: Any) -> str:
    text = f"{_cents(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {text}"


def _outdated_notice(frozen: dict[str, Any], current: dict[str, Any]) -> str:
    was, now = (frozen.get("totals") or {}).get("gain_brl"), current["totals"]["gain_brl"]
    if _cents(was) != _cents(now):
        return tr(
            f"Os dados deste mês mudaram depois da finalização: o ganho calculado hoje é {_brl(now)} "
            f"e o do relatório é {_brl(was)}. O relatório registrado não foi alterado. "
            "Gere uma nova versão para atualizar os números.",
            f"This month's data changed after the report was finalized: the gain calculated today is "
            f"{_brl(now)} and the report shows {_brl(was)}. The recorded report was not changed. "
            "Generate a new version to update the numbers.",
        )
    return tr(
        "Os dados deste mês mudaram depois da finalização (operações, valores ou custos). "
        "O relatório registrado não foi alterado. Gere uma nova versão para atualizar os números.",
        "This month's data changed after the report was finalized (operations, values or costs). "
        "The recorded report was not changed. Generate a new version to update the numbers.",
    )


def _previous_versions(data: dict[str, Any]) -> list[ReportVersionOut]:
    versions: list[ReportVersionOut] = []
    for item in data.get("previous") or []:
        try:
            versions.append(ReportVersionOut(**item))
        except (TypeError, ValueError):
            continue
    return versions


async def _detail(
    user: CurrentUser, db: Database, queue: Queue, settings: Settings, month: str
) -> ReportDetailOut:
    async with db.as_user(user.id) as conn:
        final = await _final(conn, month)
        coverage, run_limitations = await tax.load_coverage(conn)
        rows = await tax.load_rows(conn)
    month_rows = tax.rows_of(rows, month)
    totals = tax.totals_of(month_rows, settings)
    if final is not None:
        if not final["solana_sig"]:
            # a gravação na Solana ainda não aconteceu (rede fora, carteira sem saldo):
            # tentar de novo. O job_id impede tentativas em paralelo.
            await queue.enqueue("attest_report", str(final["id"]), job_id=attest_job_id(final["id"]))
        # relatório final é o que foi congelado; não é recalculado. O cálculo de hoje só
        # serve para avisar quando os dados mudaram depois (carteira relida, custo informado)
        data = cast(dict[str, Any], final["data"])
        current = _current_data(tax.report_rows(month_rows), totals)
        outdated = _signature(data) != _signature(current)
        review = _with_frozen_items(
            tax.build_review([], Totals(), coverage, run_limitations, settings), data["rows"]
        )
        if outdated:
            review = review.model_copy(
                update={"pending_reasons": [*review.pending_reasons, _outdated_notice(data, current)]}
            )
        return ReportDetailOut(
            month=month,
            status="final",
            totals=ReportTotalsOut(**data["totals"]),
            rows=data["rows"],
            attestation=_attestation(final),
            review=review,
            version=_version(final["file_url"]),
            outdated=outdated,
            current_totals=tax.to_totals(totals) if outdated else None,
            previous_versions=_previous_versions(data),
        )
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
        # o nome do arquivo baixado leva a versão ("…-v2.csv"): não se confunde com o anterior
        filename = _basename(final["file_url"])
        url, expires_at = await storage.presign(final["file_url"], filename)
        return DownloadLinkOut(url=url, filename=filename, expires_at=expires_at)

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
    filename = _basename(final["decripto_key"])
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


@router.post("/report/{month}/reissue", response_model=ReportDetailOut)
@limiter.limit("5/minute")
async def reissue_report(
    request: Request,
    month: str,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    queue: Queue = Depends(get_queue),
    settings: Settings = Depends(settings_dep),
) -> ReportDetailOut:
    """Gera uma nova versão de um relatório já finalizado, quando os dados do mês mudaram.

    A versão anterior não é apagada nem alterada: o arquivo, o hash e a transação dela
    continuam valendo e o link público dela continua abrindo, marcado como versão anterior.
    A nova versão ganha arquivo, hash, link e transação próprios.
    """
    valid_month(month)
    storage = _storage(request)
    await reissue(storage, user.id, db, queue, settings, month)
    return await _detail(user, db, queue, settings, month)


@dataclass(frozen=True)
class _Frozen:
    data: dict[str, Any]
    totals: Totals
    count: int
    csv_key: str
    decripto_key: str
    report_hash: str
    nonce: str
    public_id: str


def _reportable(rows: list[Row], settings: Settings, month: str) -> tuple[list[Row], Totals]:
    """As linhas que entram no relatório do mês, ou o erro que impede de congelá-lo."""
    if not tax.is_closed(month):
        raise AppError("month_open", 409)
    month_rows = tax.rows_of(rows, month)
    totals = tax.totals_of(month_rows, settings)
    if totals.missing_prices:
        raise AppError("missing_prices", 409, count=totals.missing_prices)
    reportable = tax.report_rows(month_rows)
    if not reportable:
        raise AppError("nothing_to_report", 409)
    return reportable, totals


async def _freeze(
    storage: Storage,
    user_id: UUID,
    db: Database,
    settings: Settings,
    month: str,
    reportable: list[Row],
    totals: Totals,
    version: int,
) -> _Frozen:
    """Grava no R2 o CSV e o resumo da versão e devolve o que vai para a tabela."""
    nonce = secrets.token_hex(32)
    csv_bytes = build_csv(reportable, totals, nonce=nonce)
    report_hash = sha256_hex(csv_bytes)
    prefix = f"{user_prefix(user_id)}{month}/"
    csv_key = f"{prefix}orbix-declare-{month}{_suffix(version)}.csv"
    decripto_key = f"{prefix}decripto-{month}{_suffix(version)}.txt"

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
    return _Frozen(
        data=_current_data(reportable, totals),
        totals=totals,
        count=len(reportable),
        csv_key=csv_key,
        decripto_key=decripto_key,
        report_hash=report_hash,
        nonce=nonce,
        public_id=public_id,
    )


async def _finalize(
    storage: Storage, user: CurrentUser, db: Database, queue: Queue, settings: Settings, month: str
) -> None:
    async with db.as_user(user.id) as conn:
        rows = await tax.load_rows(conn)
    reportable, totals = _reportable(rows, settings, month)
    frozen = await _freeze(storage, user.id, db, settings, month, reportable, totals, 1)

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
            frozen.data,
            frozen.csv_key,
            frozen.decripto_key,
            frozen.report_hash,
            bytes.fromhex(frozen.nonce),
            frozen.public_id,
            frozen.count,
            tax.RULES_VERSION,
        )
    if report_id is not None:
        await queue.enqueue("attest_report", str(report_id), job_id=attest_job_id(report_id))


async def reissue(
    storage: Storage, user_id: UUID, db: Database, queue: Queue, settings: Settings, month: str
) -> int:
    """Nova versão de um relatório final cujos dados mudaram. Devolve o número da versão.

    Recusa quando não há o que atualizar ou quando a versão atual ainda não foi registrada
    na Solana: a versão só entra no histórico imutável depois da transação confirmada, e
    substituí-la antes disso a faria desaparecer.
    """
    async with db.as_user(user_id) as conn:
        final = await _final(conn, month)
        rows = await tax.load_rows(conn)
    if final is None:
        raise AppError("report_not_final", 409)
    if not (final["solana_sig"] and final["slot"] is not None and final["attested_at"]):
        raise AppError("attestation_pending", 409)
    reportable, totals = _reportable(rows, settings, month)
    old = cast(dict[str, Any], final["data"])
    if _signature(old) == _signature(_current_data(reportable, totals)):
        raise AppError("report_up_to_date", 409)

    version = _version(final["file_url"]) + 1
    frozen = await _freeze(storage, user_id, db, settings, month, reportable, totals, version)
    # a versão que sai fica guardada com o que é preciso para o link público dela continuar
    # abrindo: identificador, hash, transação, slot e data do registro
    previous = [
        *(old.get("previous") or []),
        {
            "version": version - 1,
            "hash": final["sha256"],
            "public_id": final["public_id"],
            "tx_signature": final["solana_sig"],
            "slot": final["slot"],
            "registered_at": final["attested_at"].isoformat(),
            "finalized_at": final["finalized_at"].isoformat() if final["finalized_at"] else None,
        },
    ]
    async with db.service() as conn:
        report_id = await conn.fetchval(
            """
            update public.reports set
              total_brl = $4, gains_brl = $5, cost_brl = $6, tax_brl = $7, over_35k = $8,
              reporting_required = $8, data = $9, file_url = $10, r2_object_key = $10,
              decripto_key = $11, sha256 = $12, salt = $13, public_id = $14, events_count = $15,
              rules_version = $16, finalized_at = now(),
              solana_sig = null, slot = null, attested_at = null, attest_error = null
            where id = $1 and user_id = $2 and status = 'final' and sha256 = $3
            returning id
            """,
            final["id"],
            user_id,
            final["sha256"],
            totals.disposed,
            totals.gain,
            totals.cost,
            totals.tax,
            totals.spot_disposed > settings.exemption_limit_brl,
            {**frozen.data, "previous": previous},
            frozen.csv_key,
            frozen.decripto_key,
            frozen.report_hash,
            bytes.fromhex(frozen.nonce),
            frozen.public_id,
            frozen.count,
            tax.RULES_VERSION,
        )
    if report_id is None:
        # outra requisição gerou a nova versão no meio do caminho: a dela é a que vale
        raise AppError("report_up_to_date", 409)
    await queue.enqueue("attest_report", str(report_id), job_id=attest_job_id(report_id))
    return version


async def _free_public_id(conn: asyncpg.Connection, report_hash: str) -> str:
    # 8 caracteres do hash; em caso de colisão (com um relatório atual ou com uma versão
    # anterior, cujo link continua valendo), usa mais
    for size in range(8, 65, 2):
        candidate = report_hash[:size]
        taken = await conn.fetchval(
            "select exists(select 1 from public.reports where public_id = $1 "
            "or data->'previous' @> $2::jsonb)",
            candidate,
            [{"public_id": candidate}],
        )
        if not taken:
            return candidate
    raise not_found()
