"""Arquivos do relatório: CSV (o que tem o hash gravado na Solana) e o resumo DeCripto."""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from orbix.ingest.models import short
from orbix.tax.engine import BRT, Row, Totals

# as colunas novas entram sempre no fim: quem já lê o arquivo pelas primeiras não quebra
CSV_HEADER = (
    "data,tipo,ativo,quantidade,ptax,valor_brl,custo_brl,ganho_brl,preco_manual,"
    "taxas_brl,custo_desconhecido,rede,carteira,custo_informado"
)
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _number(value: Decimal | None, places: int) -> str:
    quantum = Decimal(1).scaleb(-places)
    return f"{(value or Decimal(0)).quantize(quantum, rounding=ROUND_HALF_UP):f}"


def _cell(value: str) -> str:
    # nome de token vem da blockchain: neutralizar fórmulas de planilha e escapar o CSV
    if value.startswith(FORMULA_PREFIXES):
        value = "'" + value
    if any(c in value for c in '",\n;'):
        return '"' + value.replace('"', '""') + '"'
    return value


def _day(moment: datetime) -> str:
    return moment.astimezone(BRT).strftime("%Y-%m-%d")


def build_csv(rows: list[Row], totals: Totals, *, nonce: str | None) -> bytes:
    """Mesmo formato do CSV que o front gera no modo demonstração, mais a linha de verificação.

    `nonce` é um código aleatório: sem ele, alguém poderia descobrir os valores do relatório
    testando combinações contra o hash público. O rascunho (nonce None) não leva linha extra:
    quem marca o arquivo como provisório é o nome dele, e uma linha solta atrapalha planilhas.

    `taxas_brl` é informativa (não entra no custo); vazia quando a fonte não informa a taxa.
    `custo_desconhecido` marca a venda cuja compra não está no histórico lido: o custo saiu
    zero e o ganho está inflado até o usuário informar o custo. `custo_informado` marca a
    venda em que o usuário digitou o custo (diferente de `preco_manual`, que é o preço da venda).
    """
    lines = [CSV_HEADER]
    for row in rows:
        lines.append(
            ",".join(
                [
                    _day(row.ts),
                    row.type,
                    _cell(row.asset),
                    _number(row.quantity, 8),
                    _number(row.ptax, 4),
                    _number(row.value, 2),
                    _number(row.cost, 2),
                    _number(row.gain, 2),
                    "sim" if row.manual else "nao",
                    _number(row.fees, 2) if row.fees is not None else "",
                    "sim" if row.cost_unknown else "nao",
                    row.chain,
                    _cell(short(row.wallet_address)),
                    "sim" if row.cost_manual else "nao",
                ]
            )
        )
    lines.append(
        ",".join(
            [
                "total",
                "",
                "",
                "",
                "",
                _number(totals.disposed, 2),
                _number(totals.cost, 2),
                _number(totals.gain, 2),
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        )
    )
    if nonce:
        lines.append(f"verificacao,{nonce}" + "," * 12)
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_decripto(
    month: str, rows: list[Row], totals: Totals, *, report_hash: str, verify_url: str, limit: Decimal
) -> bytes:
    """Resumo para apoiar o preenchimento da DeCripto. Não é o leiaute oficial da Receita."""
    over = totals.spot_disposed > limit
    lines = [
        "ORBIX DECLARE - RESUMO PARA A DECRIPTO",
        "Arquivo de apoio ao preenchimento. Nao segue o leiaute oficial de transmissao da Receita Federal.",
        "",
        f"Mes de referencia: {month}",
        f"Total alienado (R$): {_number(totals.disposed, 2)}",
        f"Total alienado em spot (R$): {_number(totals.spot_disposed, 2)}",
        f"Custo de aquisicao (R$): {_number(totals.cost, 2)}",
        f"Ganho de capital (R$): {_number(totals.gain, 2)}",
        f"Imposto estimado (R$): {_number(totals.tax, 2)}",
        f"Limite mensal de referencia (R$): {_number(limit, 2)} - "
        f"{'ultrapassado' if over else 'nao ultrapassado'}",
        f"Operacoes: {len(rows)}",
        "",
        "Conversao para reais: PTAX de venda do Banco Central do dia da operacao (ou do ultimo dia util).",
        "Custo de aquisicao: custo medio ponderado por ativo.",
        "",
        "OPERACOES",
        "data;tipo;ativo;quantidade;ptax;valor_brl;custo_brl;ganho_brl;preco_manual",
    ]
    for row in rows:
        lines.append(
            ";".join(
                [
                    _day(row.ts),
                    row.type,
                    row.asset.replace(";", " "),
                    _number(row.quantity, 8),
                    _number(row.ptax, 4),
                    _number(row.value, 2),
                    _number(row.cost, 2),
                    _number(row.gain, 2),
                    "sim" if row.manual else "nao",
                ]
            )
        )
    lines += [
        "",
        f"Hash SHA-256 do relatorio (CSV): {report_hash}",
        f"Verificacao publica: {verify_url}",
        "",
        "Os valores de imposto sao estimativas e nao substituem a orientacao de um contador.",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")
