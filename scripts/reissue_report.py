"""Nova versão de um relatório final, pela operação (sem passar pelo front).

Existe para o caso em que um erro nosso congelou um relatório errado e a tela ainda não tem o
botão "Gerar nova versão" (relatório de testes de 09/10, B18). Usa a mesma função da rota
`POST /api/report/:month/reissue`: a versão anterior fica guardada e o link público dela
continua abrindo.

Uso (na raiz do repositório, com o .env de produção):

    uv run python scripts/reissue_report.py list 2026-05
        lista os relatórios finais do mês e diz quais estão desatualizados

    uv run python scripts/reissue_report.py apply 2026-05 <id do usuário>
        gera a nova versão para essa conta

A gravação do hash na Solana é enfileirada quando o dono abrir o relatório (a fila fica na
droplet e não é alcançada daqui). Não imprime segredo nem endereço completo de carteira.
"""

import asyncio
import sys
from typing import Any, cast
from uuid import UUID

from orbix.config import get_settings
from orbix.db import Database
from orbix.errors import AppError
from orbix.routers import reports
from orbix.storage import R2Storage
from orbix.tax import service as tax


class _NoQueue:
    async def enqueue(self, job: str, *args: Any, job_id: str | None = None) -> None:
        print(f"  (gravação na Solana fica para quando o dono abrir o relatório: {job})")


def _short(address: str) -> str:
    return address if len(address) <= 12 else f"{address[:6]}…{address[-4:]}"


async def _list(db: Database, month: str) -> None:
    settings = get_settings()
    async with db.service() as conn:
        finals = await conn.fetch(
            "select id, user_id, sha256, public_id, solana_sig, file_url, data, finalized_at "
            "from public.reports where month = $1 and status = 'final' order by finalized_at",
            tax.month_date(month),
        )
    if not finals:
        print("nenhum relatório final neste mês")
        return
    for final in finals:
        user_id = final["user_id"]
        async with db.as_user(user_id) as conn:
            rows = await tax.load_rows(conn)
            wallets = await conn.fetch("select chain, address, status, sync_cursor from public.wallets")
        month_rows = tax.rows_of(rows, month)
        totals = tax.totals_of(month_rows, settings)
        current = reports._current_data(tax.report_rows(month_rows), totals)
        frozen = cast(dict[str, Any], final["data"])
        outdated = reports._signature(frozen) != reports._signature(current)
        print(
            f"\nusuário {user_id} · versão {reports._version(final['file_url'])} · link /v/{final['public_id']}"
        )
        print(
            f"  finalizado em {final['finalized_at']:%d/%m/%Y %H:%M} · registrado na Solana: {bool(final['solana_sig'])}"
        )
        print(
            f"  ganho no relatório: {frozen['totals']['gain_brl']:.2f} · ganho hoje: "
            f"{current['totals']['gain_brl']:.2f} · sem preço hoje: {totals.missing_prices} · "
            f"{'DESATUALIZADO' if outdated else 'em dia'}"
        )
        for wallet in wallets:
            cursor = wallet["sync_cursor"] or ""
            ledger = wallet["chain"] != "hyperliquid" or cursor.count(":") == 2
            print(
                f"  carteira {wallet['chain']:11} {_short(wallet['address']):12} {wallet['status']:8} "
                f"{'extrato lido' if ledger else 'EXTRATO AINDA NÃO LIDO'}"
            )


async def _apply(db: Database, month: str, user_id: UUID) -> None:
    settings = get_settings()
    storage = R2Storage(settings)
    try:
        version = await reports.reissue(storage, user_id, db, cast(Any, _NoQueue()), settings, month)
    except AppError as exc:
        print(f"recusado: {exc.code}")
        return
    print(f"versão {version} gerada para {user_id} em {month}")


async def main() -> None:
    if (
        len(sys.argv) < 3
        or sys.argv[1] not in ("list", "apply")
        or (sys.argv[1] == "apply" and len(sys.argv) < 4)
    ):
        raise SystemExit(__doc__)
    settings = get_settings()
    dsn = settings.database_url.get_secret_value()
    local = "localhost" in dsn or "127.0.0.1" in dsn
    db = Database(dsn, ssl=settings.db_ssl or not local, max_size=2)
    await db.connect()
    try:
        if sys.argv[1] == "list":
            await _list(db, sys.argv[2])
        else:
            await _apply(db, sys.argv[2], UUID(sys.argv[3]))
    finally:
        await db.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    asyncio.run(main())
