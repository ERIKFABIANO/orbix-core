from datetime import UTC, datetime
from decimal import Decimal
from itertools import count
from typing import Any

from orbix.reports.decripto_oficial import build_decripto_oficial
from orbix.tax.engine import Event, compute_rows

_ids = count(1)
T0 = datetime(2026, 9, 10, 15, 0, tzinfo=UTC)


def ev(
    kind: str,
    asset: str,
    qty: str,
    brl: str | None,
    *,
    tx: str | None = None,
    ts: datetime = T0,
    raw: dict[str, Any] | None = None,
) -> Event:
    n = next(_ids)
    return Event(
        id=f"e{n}",
        wallet_id="w1",
        chain="solana",
        tx_hash=tx or f"tx{n}",
        ts=ts,
        kind=kind,
        asset=asset,
        qty=Decimal(qty),
        brl_value=Decimal(brl) if brl is not None else None,
        ptax=Decimal("5"),
        policy="auto",
        raw=raw or {},
        symbol=asset,
    )


def swap(
    sell: tuple[str, str, str | None], buy: tuple[str, str, str | None], *, source: str | None = "JUPITER"
) -> list[Event]:
    tx = f"swap{next(_ids)}"
    raw = {"source": source} if source else {}
    return [ev("swap_out", *sell, tx=tx, raw=raw), ev("swap_in", *buy, tx=tx, raw=raw)]


def test_permuta_com_protocolo_conhecido_vira_registro_0210() -> None:
    rows = compute_rows(swap(("SOL", "5", "10000"), ("USDC", "10000", "10000")))
    result = build_decripto_oficial(rows)

    assert len(result.included) == 1
    assert result.excluded == []
    line = result.content.decode("utf-8").rstrip("\r\n")
    fields = line.split("|")
    assert fields[0] == "0210"
    assert fields[1] == "10092026"
    assert fields[2] == "II"
    assert fields[3] == "10000,00"  # OperacaoValor
    assert fields[5] == "USDC"  # CriptoativoRecebidoSimbolo (o que entrou)
    assert fields[6] == "10000,0000000000"  # CriptoativoRecebidoQuantidade
    assert fields[7] == "SOL"  # CriptoativoEntregueSimbolo (o que saiu)
    assert fields[8] == "5,0000000000"  # CriptoativoEntregueQuantidade
    assert fields[10] == "8"  # PermutanteTipoNI: Plataforma Descentralizada
    assert fields[11] == "BR"
    assert fields[15] == "Jupiter"  # PermutantePlataforma
    assert result.content.endswith(b"\r\n")


def test_permuta_sem_protocolo_identificado_fica_excluida() -> None:
    rows = compute_rows(swap(("SOL", "5", "10000"), ("USDC", "10000", "10000"), source=None))
    result = build_decripto_oficial(rows)

    assert result.included == []
    assert len(result.excluded) == 1
    assert result.content == b""


def test_eventos_fora_da_solana_sao_ignorados() -> None:
    events = swap(("SOL", "5", "10000"), ("USDC", "10000", "10000"))
    for e in events:
        object.__setattr__(e, "chain", "hyperliquid")
    rows = compute_rows(events)

    result = build_decripto_oficial(rows)

    assert result.included == []
    assert result.excluded == []
