"""Correções do status de 08 e 09/10: BOM do CSV (B16), agente (A5/A6), cota, PTAX e envio de token."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import asyncpg
import fakeredis.aioredis
import httpx
import pytest
import respx

from orbix.agent import service as agent
from orbix.agent.llm import AgentAnswer, BreakdownLine
from orbix.db import Database
from orbix.i18n import set_locale
from orbix.ingest import solana
from orbix.ingest.models import SOL, USDC_MINT
from orbix.prices import ptax
from orbix.reports.files import UTF8_BOM, build_csv
from orbix.tax.engine import Row, Totals
from orbix.worker import reprice_pending
from tests.conftest import FakeLLM, bearer, wallet_login
from tests.helpers import add_swap, seed_september, wallet_of
from tests.test_normalize import OTHER, WALLET, token_change, tx
from tests.test_sync import FakeHelius, sources

BCB = "https://olinda.bcb.gov.br/"


def row(asset: str = "KNTQ → USDC", day: int = 5, **kw: Any) -> Row:
    base: dict[str, Any] = {
        "id": f"e-{asset}-{day}",
        "ts": datetime(2026, 10, day, 15, 0, tzinfo=UTC),
        "type": "swap",
        "chain": "hyperliquid",
        "asset": asset,
        "quantity": Decimal("671.13"),
        "quantity_asset": asset.split(" ")[0],
        "tx_hash": "hl:x",
        "wallet_address": "0xabc",
        "value": Decimal("1120.97"),
        "cost": Decimal("20.00"),
        "gain": Decimal("1100.97"),
        "ptax": Decimal("4.9859"),
    }
    return Row(**(base | kw))


# ── B16 ───────────────────────────────────────────────────────────────────


def test_csv_starts_with_the_utf8_mark_so_excel_reads_the_arrow() -> None:
    content = build_csv([row()], Totals(), nonce="ab" * 32)
    assert content.startswith(UTF8_BOM) and not content[3:].startswith(UTF8_BOM)
    text = content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("data,tipo,ativo")
    assert "KNTQ → USDC" in text
    # o que o Excel fazia sem a marca: lia os bytes do UTF-8 como Windows-1252
    assert "→".encode().decode("cp1252") in content[3:].decode("cp1252")
    assert text.splitlines()[-1].startswith("verificacao,")


# ── agente ────────────────────────────────────────────────────────────────


def test_answer_is_cleaned_of_markdown_refs_and_echo() -> None:
    set_locale("en")
    refs = {"r1": row(), "r12": row("HYPE → USDC", 6)}
    dirty = AgentAnswer(
        text="Pergunta: a\n**Swap r1** cost and `r12`, see r1. ## not a heading",
        breakdown=[BreakdownLine(label="**total** r12", value="__R$ 1,00__", emphasis="total")],
        cited_rows=["r1"],
        suggestions=["What happened in r1?"],
    )
    clean = agent.sanitize(dirty, refs)
    assert (
        clean.text
        == "a\nSwap KNTQ → USDC on 05/10 cost and HYPE → USDC on 06/10, see KNTQ → USDC on 05/10. ## not a heading"
    )
    assert (clean.breakdown[0].label, clean.breakdown[0].value) == ("total HYPE → USDC on 06/10", "R$ 1,00")
    assert clean.suggestions == ["What happened in KNTQ → USDC on 05/10?"]
    assert clean.cited_rows == ["r1"]  # as referências continuam valendo para montar as citações
    heading = agent.sanitize(
        AgentAnswer(text="# Resumo\ntexto", breakdown=[], cited_rows=[], suggestions=[]), {}
    )
    assert heading.text == "Resumo\ntexto"
    set_locale("pt")


def test_question_about_one_line_is_recognized() -> None:
    kntq, dust, hype = row(), row(day=6, id="dust"), row("HYPE → USDC", 5)
    rows = [kntq, dust, hype]
    assert agent.find_row("por que o HYPE -> USDC deu ganho?", rows) is hype
    assert agent.find_row("explica o swap KNTQ → USDC de 06/10", rows) is dust
    assert agent.find_row("kntq usdc 5/10", rows) is kntq
    # dois candidatos e nenhuma data: não escolhe um
    assert agent.find_row("e o KNTQ USDC?", rows) is None
    assert agent.find_row("quanto paguei de imposto?", rows) is None
    assert agent.find_row("a", rows) is None
    # o id do evento vale mais que o texto; id de outro mês não acha nada
    assert agent.find_row("explica", rows, "dust") is dust
    assert agent.find_row("HYPE USDC", rows, "nao-existe") is None


def test_rules_answer_explains_the_asked_line() -> None:
    set_locale("pt")
    line = row(
        avg_cost_unit=Decimal("0.0298"), position_before_qty=Decimal("671.13907462"), fees=Decimal("0.5")
    )
    answer = agent.rules_answer("2026-10", [line], Totals(), {"r1": line}, line)
    assert answer.text.startswith("KNTQ → USDC de 05/10: saíram 671,13 KNTQ, no valor de R$ 1.120,97.")
    assert "custo médio" in answer.text and "R$ 1.120,97 - R$ 20,00 = R$ 1.100,97" in answer.text
    assert "Taxas de R$ 0,50" in answer.text and "não substitui um contador" in answer.text
    assert answer.cited_rows == ["r1"]
    assert [(b.label, b.value, b.emphasis) for b in answer.breakdown] == [
        ("Valor da venda", "R$ 1.120,97", "none"),
        ("Custo de aquisição", "R$ 20,00", "none"),
        ("Resultado", "R$ 1.100,97", "gain"),
    ]
    unknown = agent.rules_answer("2026-10", [line], Totals(), {}, row(cost_unknown=True, cost=Decimal(0)))
    assert "não está no histórico lido" in unknown.text and unknown.cited_rows == []
    informed = agent.rules_answer("2026-10", [line], Totals(), {}, row(cost_manual=True))
    assert "foi informado por você" in informed.text
    funding = row(
        "HYPE-PERP", type="funding", quantity=Decimal("1.25"), gain=Decimal("-6.23"), cost=Decimal("6.23")
    )
    assert "funding pago de 1,25 USDC" in agent.rules_answer("2026-10", [], Totals(), {}, funding).text
    set_locale("en")
    english = agent.rules_answer("2026-10", [line], Totals(), {"r1": line}, line)
    assert english.text.startswith("KNTQ → USDC on 05/10: 671,13 KNTQ went out")
    set_locale("pt")


def test_prompt_carries_the_focused_line_and_what_the_app_knows() -> None:
    from orbix.config import get_settings

    set_locale("pt")
    line = row(
        fees=Decimal("0.5"),
        fills=3,
        quantity_in=Decimal("224.83"),
        quantity_in_asset="USDC",
        position_before_qty=Decimal("671.13907462"),
        avg_cost_unit=Decimal("0.0298"),
        unit_price=Decimal("1.6703"),
        ptax_date=date(2026, 10, 5),
    )
    others = [row(f"T{i} → USDC", 7, id=f"big{i}", gain=Decimal(5000 + i)) for i in range(agent.MAX_ROWS)]
    system, user, refs = agent.build_prompt(
        get_settings(), "2026-10", "draft", [*others, line], Totals(), Totals(), "explica", line
    )
    import json

    data = json.loads(user.split("<dados>\n", 1)[1].split("\n</dados>", 1)[0])
    focused = next(item for item in data["linhas"] if item["ref"] == data["linha_em_foco"])
    assert refs[data["linha_em_foco"]] is line  # entra mesmo sendo a de menor ganho
    assert (focused["taxas_brl"], focused["execucoes"], focused["recebido"]) == (0.5, 3, "224.83 USDC")
    assert (focused["posicao_antes"], focused["custo_medio_unitario_brl"]) == ("671.13907462", "0.0298")
    assert (focused["data_ptax"], focused["rede"], focused["unidade"]) == (
        "2026-10-05",
        "hyperliquid",
        "KNTQ",
    )
    assert "execucoes" not in data["linhas"][-1]  # campo vazio não gasta contexto
    for rule in ("sem markdown", "linha_em_foco", "leiaute", "stablecoins", "não prova que o"):
        assert rule in system, rule
    assert (
        "linha_em_foco"
        not in agent.build_prompt(get_settings(), "2026-10", "draft", [line], Totals(), Totals(), "oi")[1]
    )


async def _september(client: httpx.AsyncClient, admin: asyncpg.Connection) -> tuple[dict[str, str], str, str]:
    session = await wallet_login(client)
    seeded = await seed_september(admin, session["user"]["id"])
    return bearer(session["token"]), session["user"]["id"], seeded["sale_id"]


async def test_rules_reply_explains_the_event_when_the_model_is_down(
    client: httpx.AsyncClient, admin: asyncpg.Connection
) -> None:
    headers, _, sale_id = await _september(client, admin)
    client.app.state.llm = None  # type: ignore[attr-defined]
    for body in (
        {"message": "explica", "month": "2026-09", "eventId": sale_id},
        {"message": "por que o SOL → USDC de 20/09 deu ganho?", "month": "2026-09"},
    ):
        reply = (await client.post("/api/agent", json=body, headers=headers)).json()
        assert reply["message"]["source"] == "rules", reply
        text = reply["message"]["blocks"][0]["text"]
        assert text.startswith("SOL → USDC de 20/09: saíram 4 SOL, no valor de R$ 4.000,00."), text
        assert "R$ 4.000,00 - R$ 2.000,00 = R$ 2.000,00" in text
        assert reply["message"]["blocks"][1]["rows"][-1] == {
            "label": "Resultado",
            "value": "R$ 2.000,00",
            "emphasis": "gain",
        }
        assert reply["context"]["sourceTx"]["signature"] == "5hN2saleSignature"
    # sem linha reconhecida, continua o resumo do mês
    summary = (
        await client.post("/api/agent", json={"message": "resumo", "month": "2026-09"}, headers=headers)
    ).json()
    assert summary["message"]["blocks"][0]["text"].startswith("Em setembro de 2026 foram 2 operação(ões)")
    assert (
        await client.post("/api/agent", json={"message": "x", "eventId": "y" * 65}, headers=headers)
    ).status_code == 422


async def test_model_reply_is_cleaned_before_it_reaches_the_screen(
    client: httpx.AsyncClient, admin: asyncpg.Connection, llm: FakeLLM
) -> None:
    headers, _, _ = await _september(client, admin)
    llm.answer_value = AgentAnswer(
        text="Pergunta: a\nO **Swap r2** teve ganho.",
        breakdown=[BreakdownLine(label="**gain**", value="R$ 2.000,00", emphasis="gain")],
        cited_rows=["r2"],
        suggestions=["O que houve em r2?"],
    )
    reply = (
        await client.post("/api/agent", json={"message": "a", "month": "2026-09"}, headers=headers)
    ).json()
    assert reply["message"]["blocks"][0]["text"] == "a\nO Swap SOL → USDC de 20/09 teve ganho."
    assert reply["message"]["blocks"][1]["rows"][0]["label"] == "gain"
    assert reply["suggestions"] == ["O que houve em SOL → USDC de 20/09?"]


async def test_two_questions_at_once_with_one_left_call_the_model_once(
    client: httpx.AsyncClient, admin: asyncpg.Connection, llm: FakeLLM
) -> None:
    headers, user, _ = await _september(client, admin)
    await admin.execute(
        "update public.profiles set agent_questions_used = 19, agent_period = date_trunc('month', now())::date"
    )
    original = llm.answer

    async def slow(system: str, messages: list[dict[str, str]]) -> AgentAnswer:
        await asyncio.sleep(0.3)
        return await original(system, messages)

    llm.answer = slow  # type: ignore[method-assign]
    first, second = await asyncio.gather(
        client.post("/api/agent", json={"message": "uma", "month": "2026-09"}, headers=headers),
        client.post("/api/agent", json={"message": "outra", "month": "2026-09"}, headers=headers),
    )
    replies = [first.json(), second.json()]
    assert (first.status_code, second.status_code) == (200, 200)
    assert sorted(r["message"]["source"] for r in replies) == ["ai", "rules"]
    assert next(r for r in replies if r["message"]["source"] == "rules")["rulesReason"] == "quota"
    assert [r["questionsLeft"] for r in replies] == [0, 0]
    assert len(llm.calls) == 1  # antes as duas passavam pela checagem e as duas pagavam o modelo
    assert await admin.fetchval("select agent_questions_used from public.profiles where id = $1", user) == 20


async def test_question_is_given_back_when_the_model_breaks(
    client: httpx.AsyncClient, admin: asyncpg.Connection, llm: FakeLLM
) -> None:
    from orbix.errors import AppError

    headers, user, _ = await _september(client, admin)

    async def unavailable(system: str, messages: list[dict[str, str]]) -> AgentAnswer:
        raise AppError("agent_unavailable", 503)

    llm.answer = unavailable  # type: ignore[method-assign]
    reply = (
        await client.post("/api/agent", json={"message": "oi", "month": "2026-09"}, headers=headers)
    ).json()
    assert (reply["message"]["source"], reply["rulesReason"], reply["questionsLeft"]) == (
        "rules",
        "unavailable",
        20,
    )
    assert await admin.fetchval("select agent_questions_used from public.profiles where id = $1", user) == 0

    async def crash(system: str, messages: list[dict[str, str]]) -> AgentAnswer:
        raise RuntimeError("inesperado")

    llm.answer = crash  # type: ignore[method-assign]
    try:
        failed = await client.post("/api/agent", json={"message": "oi", "month": "2026-09"}, headers=headers)
        assert failed.status_code == 500
    except RuntimeError:
        pass
    # erro inesperado também não fica com a pergunta do usuário
    assert await admin.fetchval("select agent_questions_used from public.profiles where id = $1", user) == 0


async def test_model_failures_that_are_not_api_errors_fall_back_to_rules() -> None:
    """Resposta cortada por tamanho e resposta barrada pelo filtro não são APIError: viravam 500."""
    from openai import LengthFinishReasonError

    from orbix.agent.llm import OpenAIAgent
    from orbix.errors import AppError

    model = OpenAIAgent("sk-test", "gpt-test")

    class Completions:
        def __init__(self, outcome: Any) -> None:
            self.outcome = outcome

        async def parse(self, **_: Any) -> Any:
            if isinstance(self.outcome, Exception):
                raise self.outcome
            return self.outcome

    class Empty:
        usage = None
        choices: tuple[Any, ...] = ()

    cut = LengthFinishReasonError.__new__(LengthFinishReasonError)
    for outcome in (cut, Empty()):
        model._client.chat.completions = Completions(outcome)  # type: ignore[assignment]
        with pytest.raises(AppError) as caught:
            await model.answer("sistema", [{"role": "user", "content": "oi"}])
        assert (caught.value.code, caught.value.status) == ("agent_unavailable", 503)
    await model.close()


# ── PTAX ──────────────────────────────────────────────────────────────────


@respx.mock
async def test_central_bank_is_asked_without_select(http_out: httpx.AsyncClient) -> None:
    """Em 09/10/2026 o Banco Central passou a responder 403 quando a consulta leva $select."""
    route = respx.get(url__startswith=BCB).respond(
        json={
            "value": [
                {"cotacaoCompra": 5.19, "cotacaoVenda": 5.2, "dataHoraCotacao": "2026-10-06 13:03:18.656"}
            ]
        }
    )
    rates = await ptax.fetch(http_out, date(2026, 10, 1), date(2026, 10, 8))
    assert [(r[0], r[2]) for r in rates] == [(date(2026, 10, 6), Decimal("5.2"))]
    query = dict(route.calls.last.request.url.params)
    assert "$select" not in query
    assert query == {"@dataInicial": "'10-01-2026'", "@dataFinalCotacao": "'10-08-2026'", "$format": "json"}


@respx.mock
async def test_missing_rate_of_the_day_is_not_asked_again_right_away(
    admin: asyncpg.Connection, http_out: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ptax._tail_attempts.clear()
    today = datetime.now(ptax.BRT).date()
    # um dia de semana recente em que "ainda não saiu" a cotação
    end = today - timedelta(days=(today.weekday() - 4) % 7 or 7) if today.weekday() > 4 else today
    last = end - timedelta(days=1)
    await admin.execute("delete from public.fx_rates")
    await admin.execute(
        "insert into public.fx_rates (date, ptax_buy, ptax_sell) select d::date, 5.19, 5.2 "
        "from generate_series($1::date, $2::date, interval '1 day') d",
        end - timedelta(days=15),
        last,
    )
    stale = respx.get(url__startswith=BCB).respond(
        json={
            "value": [{"cotacaoCompra": 5.19, "cotacaoVenda": 5.2, "dataHoraCotacao": f"{last} 13:03:18.656"}]
        }
    )
    for _ in range(3):
        await ptax.ensure_range(admin, http_out, end, end)
    assert stale.call_count == 1  # antes: uma consulta por sincronização até a PTAX sair

    # passado o intervalo, pergunta de novo; quando a cotação chega, é gravada
    monkeypatch.setattr(ptax, "RETRY_AFTER", 0)
    fresh = respx.get(url__startswith=BCB).respond(
        json={
            "value": [{"cotacaoCompra": 5.29, "cotacaoVenda": 5.3, "dataHoraCotacao": f"{end} 13:03:18.656"}]
        }
    )
    await ptax.ensure_range(admin, http_out, end, end)
    assert fresh.call_count == 2  # a mesma rota do respx: 1 de antes + 1 agora
    assert await admin.fetchval("select ptax_sell from public.fx_rates where date = $1", end) == Decimal(
        "5.3"
    )
    monkeypatch.undo()
    await ptax.ensure_range(admin, http_out, end, end)
    assert fresh.call_count == 2  # janela coberta: nem consulta
    ptax._tail_attempts.clear()


@respx.mock
async def test_first_read_of_an_empty_table_always_asks(
    admin: asyncpg.Connection, http_out: httpx.AsyncClient
) -> None:
    """A espera só vale para a ponta final: sem cotação nenhuma, cada chamada consulta."""
    ptax._tail_attempts.clear()
    await admin.execute("delete from public.fx_rates")
    route = respx.get(url__startswith=BCB).respond(json={"value": []})
    day = date(2026, 9, 15)
    await ptax.ensure_range(admin, http_out, day, day)
    await ptax.ensure_range(admin, http_out, day, day)
    assert route.call_count == 2


@respx.mock
async def test_rate_correction_shows_in_the_event_history_and_reprice_survives_a_ptax_failure(
    client: httpx.AsyncClient,
    admin: asyncpg.Connection,
    worker_db: Database,
    redis: fakeredis.aioredis.FakeRedis,
    http_out: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ptax._tail_attempts.clear()
    session = await wallet_login(client)
    user, headers = session["user"]["id"], bearer(session["token"])
    wallet = await wallet_of(admin, user)
    await admin.execute(
        "insert into public.assets (chain, asset, symbol, is_stable) values "
        "('solana', 'SOL', 'SOL', false), ('solana', $1, 'USDC', true) on conflict do nothing",
        USDC_MINT,
    )
    ts = datetime.now(UTC) - timedelta(days=2)
    day = ts.astimezone(ptax.BRT).date()
    await add_swap(admin, user, wallet, ("SOL", "1", "500"), (USDC_MINT, "100", "500"), ts=ts)
    await admin.execute("delete from public.fx_rates")
    await admin.execute(
        "update public.events set usd_price = case when asset = $2 then 1 else 100 end, ptax = 5.0, "
        "ptax_date = $1",
        day - timedelta(days=1),
        USDC_MINT,
    )
    await admin.execute("delete from public.event_reviews")
    respx.get(url__startswith=BCB).respond(
        json={
            "value": [{"cotacaoCompra": 5.19, "cotacaoVenda": 5.2, "dataHoraCotacao": f"{day} 13:03:18.656"}]
        }
    )
    ctx = {"db": worker_db, "redis": redis, "sources": sources(http_out, FakeHelius([]))}
    await reprice_pending(ctx)

    month = ts.astimezone(ptax.BRT).strftime("%Y-%m")
    (event,) = (await client.get("/api/events", params={"month": month}, headers=headers)).json()
    (review,) = event["reviewHistory"]
    old, new = (day - timedelta(days=1)).strftime("%d/%m/%Y"), day.strftime("%d/%m/%Y")
    assert review["kind"] == "price"
    assert review["reason"].startswith(
        f"Correção automática do câmbio: a PTAX de {old} foi trocada pela de {new}"
    )
    assert review["evidence"].startswith("PTAX de venda 5.0") and "5.2" in review["evidence"]
    assert (review["previousPriceBrl"], review["newPriceBrl"]) == (500.0, 520.0)
    english = (
        await client.get("/api/events", params={"month": month}, headers={**headers, "Accept-Language": "en"})
    ).json()
    assert english[0]["reviewHistory"][0]["reason"].startswith("Automatic exchange-rate correction")

    # a correção da PTAX quebrou: os eventos sem preço ainda são cotados
    await admin.execute("update public.events set brl_value = null, ptax = null, ptax_date = null")

    async def broken(*_: Any) -> int:
        raise RuntimeError("banco central fora do ar")

    monkeypatch.setattr(ptax, "refresh_stale", broken)
    respx.get(url__startswith="https://api.coingecko.com/").respond(json={"prices": []})
    await reprice_pending(ctx)
    assert await admin.fetchval("select brl_value from public.events where asset = $1", USDC_MINT) == Decimal(
        520
    )
    ptax._tail_attempts.clear()


# ── Solana ────────────────────────────────────────────────────────────────


def test_first_token_send_pays_rent_as_fee_not_as_a_second_transfer() -> None:
    """Quem envia um token pela primeira vez paga o aluguel da conta de token do destinatário.
    Isso aparecia como uma saída de SOL cuja contraparte era a conta de token."""
    token_account = "7UX2i7SucgLMQcfZ75s3VXmZZY4YRUyJN9X1RgfMoDUi"
    sent = solana.normalize_transaction(
        tx(
            "TRANSFER",
            native=-2_044_280,
            changes=[token_change(WALLET, USDC_MINT, "-50000000", 6)],
            nativeTransfers=[
                {"fromUserAccount": WALLET, "toUserAccount": token_account, "amount": 2_039_280}
            ],
            tokenTransfers=[{"fromUserAccount": WALLET, "toUserAccount": OTHER, "mint": USDC_MINT}],
        ),
        WALLET,
    )
    assert [(e.kind, e.asset, e.qty) for e in sent] == [
        ("transfer_out", USDC_MINT, Decimal("50")),
        ("fee", SOL, Decimal("0.00204428")),
    ]
    assert sent[0].raw["counterparty"] == OTHER
    assert all(e.raw.get("counterparty") != token_account for e in sent)


def test_sol_sent_together_with_a_token_is_still_a_transfer() -> None:
    both = solana.normalize_transaction(
        tx(
            "TRANSFER",
            native=-500_005_000,
            changes=[token_change(WALLET, USDC_MINT, "-50000000", 6)],
        ),
        WALLET,
    )
    assert [(e.kind, e.asset, e.qty) for e in both] == [
        ("transfer_out", USDC_MINT, Decimal("50")),
        ("transfer_out", SOL, Decimal("0.5")),
        ("fee", SOL, Decimal("0.000005")),
    ]
    # SOL pequeno sozinho continua sendo um envio de SOL
    small = solana.normalize_transaction(tx("TRANSFER", native=-2_005_000), WALLET)
    assert [(e.kind, e.asset, e.qty) for e in small] == [
        ("transfer_out", SOL, Decimal("0.002")),
        ("fee", SOL, Decimal("0.000005")),
    ]
