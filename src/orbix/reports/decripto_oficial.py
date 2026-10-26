"""Arquivo oficial da DeCripto (RFB), leiaute da Seção 6 do Manual de Orientação (ADE Copes
nº 02/2025) — "Leiaute do Arquivo Digital a Ser Entregue pelas Pessoas Físicas e Jurídicas
SEM o Uso de Prestador de Serviço de Criptoativo".

Cobre hoje só o registro 0210 (permuta entre criptoativos declaráveis), porque é o que
de fato acontece numa troca on-chain na Solana: toda operação é cripto-por-cripto, mesmo
contra uma stablecoin, que para a Receita também é criptoativo declarável, não "reais".
Sem uma perna em moeda fiduciária no modelo de dados (não há integração bancária), os
registros 0110/0120 (compra/venda em reais) nunca teriam como ser preenchidos e por isso
não são gerados.

Cada permuta identifica a contraparte como "Plataforma Descentralizada" (TipoNI = 8, único
código da tabela oficial que cobre uma troca num pool/DEX sem pessoa do outro lado) quando
o protocolo da troca é conhecido (ex.: Jupiter, Raydium). Esse campo é obrigatório no
leiaute e não existe código "desconhecido" nele: uma troca cujo protocolo não foi
identificado, ou que envolveu mais de dois ativos numa só operação, não tem como ser
declarada corretamente e fica de fora do arquivo — listada em `excluded`, para o chamador
avisar o usuário em vez de gerar uma linha fiscalmente incorreta.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from orbix.tax.engine import Row

CRLF = "\r\n"


def _alfanumerico(value: str | None, max_len: int) -> str:
    if not value:
        return ""
    # o leiaute proíbe o delimitador "|" e caracteres não imprimíveis dentro do campo
    cleaned = "".join(ch for ch in value if ch != "|" and ord(ch) >= 32)
    return cleaned[:max_len]


def _numerico(value: Decimal | None, casas: int) -> str:
    if value is None:
        return ""
    quantum = Decimal(1).scaleb(-casas)
    return f"{value.quantize(quantum, rounding=ROUND_HALF_UP):f}".replace(".", ",")


def _data(moment: datetime) -> str:
    return moment.strftime("%d%m%Y")


def _elegivel_0210(row: Row) -> bool:
    """Só vira registro 0210 a permuta com um único ativo de cada lado e protocolo conhecido."""
    return (
        row.type == "swap"
        and row.chain == "solana"
        and row.quantity_in is not None
        and row.quantity_in_asset is not None
        and bool(row.protocol)
    )


def _registro_0210(row: Row) -> str:
    fields = [
        "0210",
        _data(row.ts),
        "II",  # OperacaoCodigo: permuta entre criptoativos declaráveis
        _numerico(row.value, 2),
        _numerico(row.fees, 2),
        _alfanumerico(row.quantity_in_asset, 10),
        _numerico(row.quantity_in, 10),
        _alfanumerico(row.quantity_asset, 10),
        _numerico(row.quantity, 10),
        "",  # AvaliacaoAlternativaValor: não usamos critério de avaliação alternativo
        "8",  # PermutanteTipoNI: Plataforma Descentralizada
        "BR",  # PermutantePais
        "",  # PermutanteCPFCNPJ
        "",  # PermutanteNI
        "",  # PermutanteNome
        _alfanumerico(row.protocol, 80),  # PermutantePlataforma
    ]
    return "|".join(fields)


@dataclass(frozen=True)
class DecriptoOficialResult:
    content: bytes
    included: list[Row]
    excluded: list[Row]


def build_decripto_oficial(rows: list[Row]) -> DecriptoOficialResult:
    """Gera o arquivo oficial (Seção 6, registro 0210) a partir das linhas do relatório.

    Linhas que não são permuta na Solana (Hyperliquid, transferência, perda etc.) são
    simplesmente ignoradas — não entram nem em `included` nem em `excluded`, porque não são
    candidatas a este leiaute. `excluded` só lista permutas que seriam elegíveis, mas faltou
    informação para identificar a contraparte corretamente.
    """
    swaps = [r for r in rows if r.type == "swap" and r.chain == "solana"]
    included = [r for r in swaps if _elegivel_0210(r)]
    excluded = [r for r in swaps if r not in included]
    lines = [_registro_0210(r) for r in included]
    content = (CRLF.join(lines) + CRLF).encode("utf-8") if lines else b""
    return DecriptoOficialResult(content=content, included=included, excluded=excluded)
