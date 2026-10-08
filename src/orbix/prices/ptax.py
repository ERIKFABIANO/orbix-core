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


def _has_weekday_after(last: date, end: date) -> bool:
    """Existe dia de semana depois de `last`, até `end`? (dia em que pode ter saído PTAX)"""
    day = last + timedelta(days=1)
    while day <= end:
        if day.weekday() < 5:
            return True
        day += timedelta(days=1)
    return False


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
    # janela já coberta nas duas pontas: nada a buscar. Na ponta final só fim de semana pode
    # faltar. Antes havia uma folga de 4 dias aqui: com a PTAX de sexta guardada, a de segunda
    # e a de terça nunca eram buscadas, e o evento de terça saía com o câmbio de sexta (B15).
    if (
        bounds
        and bounds["n"]
        and bounds["lo"] <= start + LOOKBACK
        and not _has_weekday_after(bounds["hi"], end)
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


# até quantos dias depois da operação a PTAX certa ainda é procurada
STALE_WINDOW = timedelta(days=20)


async def refresh_stale(conn: asyncpg.Connection, http: httpx.AsyncClient) -> int:
    """Corrige eventos que ficaram com a PTAX de um dia anterior ao da operação.

    A PTAX do dia sai por volta das 13h. Um evento lido antes disso (ou com a tabela de
    câmbio desatualizada) recebe a do último dia útil anterior, e nada o corrigia depois.
    Aqui, quando a cotação do dia da operação (ou uma mais próxima dele) já existe, o evento
    passa a usá-la e o valor em reais é refeito. Preço manual não é tocado, e relatório já
    finalizado não muda: ele guarda os próprios números.
    """
    today = datetime.now(BRT).date()
    await ensure_range(conn, http, today - STALE_WINDOW, today)
    status = await conn.execute(
        """
        with fresh as (
            select e.id, f.date, f.ptax_sell
              from public.events e
              join lateral (
                    select date, ptax_sell from public.fx_rates
                     where date <= (e.ts at time zone 'America/Sao_Paulo')::date
                     order by date desc limit 1
                   ) f on true
             where e.ptax_date is not null and e.usd_price is not null
               and e.pricing_policy is distinct from 'manual'
               and e.ts >= $1
               and f.date > e.ptax_date
        )
        update public.events e
           set ptax = fresh.ptax_sell, ptax_date = fresh.date,
               brl_value = e.qty * e.usd_price * fresh.ptax_sell,
               review_reason = 'cotação automática'
          from fresh
         where e.id = fresh.id
        """,
        datetime.now(BRT) - STALE_WINDOW,
    )
    return int(status.rsplit(" ", 1)[-1])


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
