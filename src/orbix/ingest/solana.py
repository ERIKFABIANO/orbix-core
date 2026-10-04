"""Transforma uma transação decodificada pela Helius em eventos da carteira.

A classificação parte da variação líquida de saldo da carteira (`accountData`), não da
descrição em texto: o que saiu, o que entrou e a taxa paga. Texto vindo da blockchain
(descrições, nomes de token) é tratado só como dado.
"""

from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from orbix.ingest.models import SOL, WSOL_MINT, EventDraft

LAMPORTS = Decimal(1_000_000_000)
# abaixo disso é poeira (spam de airdrop, arredondamento)
MIN_QTY = Decimal("0.000000001")
MIN_SOL = Decimal("0.00001")
# aluguel de conta de token e gorjetas de prioridade aparecem como SOL saindo num swap
# de token por token; não é uma perna do swap
RENT_NOISE_SOL = Decimal("0.02")

STAKE_TYPES = frozenset({"STAKE_SOL", "STAKE_TOKEN", "INIT_STAKE", "DEPOSIT"})
UNSTAKE_TYPES = frozenset({"UNSTAKE_SOL", "UNSTAKE_TOKEN", "WITHDRAW", "DEACTIVATE_STAKE"})
REWARD_TYPES = frozenset({"CLAIM_REWARDS", "HARVEST_REWARD", "CLAIM", "PAYOUT"})


def _balance_deltas(tx: dict[str, Any], wallet: str) -> dict[str, Decimal]:
    deltas: dict[str, Decimal] = defaultdict(Decimal)
    for account in tx.get("accountData") or []:
        if account.get("account") == wallet:
            deltas[SOL] += Decimal(int(account.get("nativeBalanceChange") or 0)) / LAMPORTS
        for change in account.get("tokenBalanceChanges") or []:
            if change.get("userAccount") != wallet:
                continue
            raw = change.get("rawTokenAmount") or {}
            try:
                amount = Decimal(str(raw["tokenAmount"])) / (Decimal(10) ** int(raw["decimals"]))
            except (KeyError, ValueError, ArithmeticError):
                continue
            mint = str(change.get("mint") or "")
            if mint:
                # SOL embrulhado (wSOL) é SOL para fins de posição e preço
                deltas[SOL if mint == WSOL_MINT else mint] += amount
    return deltas


def normalize_transaction(tx: dict[str, Any], wallet: str) -> list[EventDraft]:
    signature = str(tx.get("signature") or "")
    timestamp = tx.get("timestamp")
    if not signature or not isinstance(timestamp, int):
        return []
    ts = datetime.fromtimestamp(timestamp, UTC)
    tx_type = str(tx.get("type") or "UNKNOWN")[:40]
    raw = {
        "type": tx_type,
        "source": str(tx.get("source") or "")[:40],
        "slot": tx.get("slot") if isinstance(tx.get("slot"), int) else None,
    }

    fee = Decimal(0)
    if tx.get("feePayer") == wallet:
        fee = Decimal(int(tx.get("fee") or 0)) / LAMPORTS

    deltas = _balance_deltas(tx, wallet)
    # a taxa já está dentro da variação de SOL; separar para virar evento próprio
    deltas[SOL] += fee

    outs: dict[str, Decimal] = {}
    ins: dict[str, Decimal] = {}
    failed = tx.get("transactionError") is not None
    if not failed:
        for asset, delta in deltas.items():
            floor = MIN_SOL if asset == SOL else MIN_QTY
            if delta <= -floor:
                outs[asset] = -delta
            elif delta >= floor:
                ins[asset] = delta

        token_out = any(asset != SOL for asset in outs)
        token_in = any(asset != SOL for asset in ins)
        if token_out and token_in:
            for side in (outs, ins):
                if side.get(SOL, RENT_NOISE_SOL) < RENT_NOISE_SOL:
                    del side[SOL]

    if outs and ins:
        out_kind, in_kind = "swap_out", "swap_in"
    elif ins:
        out_kind = "transfer_out"
        in_kind = (
            "reward" if tx_type in REWARD_TYPES else "unstake" if tx_type in UNSTAKE_TYPES else "transfer_in"
        )
    else:
        out_kind = "stake" if tx_type in STAKE_TYPES else "transfer_out"
        in_kind = "transfer_in"

    events: list[EventDraft] = []

    def add(kind: str, asset: str, qty: Decimal) -> None:
        events.append(
            EventDraft(
                tx_hash=signature,
                event_index=len(events),
                ts=ts,
                kind=kind,
                asset=asset,
                qty=qty,
                raw=raw,
            )
        )

    for asset in sorted(outs):
        add(out_kind, asset, outs[asset])
    for asset in sorted(ins):
        add(in_kind, asset, ins[asset])
    if fee > 0:
        add("fee", SOL, fee)
    return events
