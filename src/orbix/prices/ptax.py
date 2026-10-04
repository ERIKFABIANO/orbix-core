"""PTAX do Banco Central (dólar oficial). API pública, sem chave."""

from bisect import bisect_right
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import asyncpg
import httpx
import structlog

log = structlog.get_logger()

BRT = ZoneInfo("America/Sao_Paulo")
URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
)
# feriados prolongados: olhar alguns dias para trás garante achar o último dia útil
LOOKBACK = timedelta(days=10)


def brt_date(moment: datetime) -> date:
    return moment.astimezone(BRT).date()


async def fetch(
    http: httpx.AsyncClient, start: date, end: date
) -> list[tuple[date, Decimal, Decimal, datetime]]:
    params = {
        "@dataInicial": f"'{start:%m-%d-%Y}'",
        "@dataFinalCotacao": f"'{end:%m-%d-%Y}'",
        "$format": "json",
        "$select": "cotacaoCompra,cotacaoVenda,dataHoraCotacao",
    }
    response = await http.get(URL, params=params, timeout=20)
    response.raise_for_status()
    rows: dict[date, tuple[date, Decimal, Decimal, datetime]] = {}
    for item in response.json().get("value", []):
        quoted = datetime.fromisoformat(str(item["dataHoraCotacao"])).replace(tzinfo=BRT)
        # em dias com mais de um boletim, fica o último (fechamento)
        rows[quoted.date()] = (
            quoted.date(),
            Decimal(str(item["cotacaoCompra"])),
            Decimal(str(item["cotacaoVenda"])),
            quoted,
        )
    return sorted(rows.values())


async def ensure_range(conn: asyncpg.Connection, http: httpx.AsyncClient, start: date, end: date) -> None:
    """Garante cotações de `start - LOOKBACK` até `end` em fx_rates."""
    start -= LOOKBACK
    end = min(end, datetime.now(BRT).date())
    bounds = await conn.fetchrow(
        "select min(date) as lo, max(date) as hi, count(*) as n from public.fx_rates "
        "where date between $1 and $2",
        start,
        end,
    )
    # janela já coberta nas duas pontas (com folga de fim de semana e feriado): nada a buscar
    if (
        bounds
        and bounds["n"]
        and bounds["lo"] <= start + LOOKBACK
        and bounds["hi"] >= end - timedelta(days=4)
    ):
        return
    try:
        rows = await fetch(http, start, end)
    except (httpx.HTTPError, ValueError, KeyError):
        log.warning("ptax: falha ao consultar o Banco Central")
        return
    await conn.executemany(
        "insert into public.fx_rates (date, ptax_buy, ptax_sell, quoted_at) values ($1, $2, $3, $4) "
        "on conflict (date) do update set ptax_buy = excluded.ptax_buy, "
        "ptax_sell = excluded.ptax_sell, quoted_at = excluded.quoted_at, fetched_at = now()",
        rows,
    )


class PtaxTable:
    """Cotação de venda do dia, ou do último dia útil anterior."""

    def __init__(self, rows: list[tuple[date, Decimal]]) -> None:
        self._dates = [d for d, _ in rows]
        self._rates = [r for _, r in rows]

    @classmethod
    async def load(cls, conn: asyncpg.Connection, start: date, end: date) -> "PtaxTable":
        rows = await conn.fetch(
            "select date, ptax_sell from public.fx_rates where date between $1 and $2 order by date",
            start - LOOKBACK,
            end,
        )
        return cls([(r["date"], r["ptax_sell"]) for r in rows])

    def lookup(self, day: date) -> tuple[date, Decimal] | None:
        index = bisect_right(self._dates, day) - 1
        if index < 0 or day - self._dates[index] > LOOKBACK:
            return None
        return self._dates[index], self._rates[index]
