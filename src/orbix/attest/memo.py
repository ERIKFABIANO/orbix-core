"""Grava e confere o hash do relatório numa transação de Memo na Solana.

Só o hash vai para a blockchain. Nenhum dado pessoal, endereço ou valor.
"""

import asyncio
import base64
import json
from typing import Any

import httpx
import structlog
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

log = structlog.get_logger()

MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")
CONFIRM_TIMEOUT = 75


class AttestError(Exception):
    pass


def memo_text(report_hash: str) -> str:
    return f"orbix-declare:v1:{report_hash}"


def load_keypair(secret: str) -> Keypair:
    """Aceita a chave em base58 ou no formato de lista JSON do `solana-keygen`."""
    secret = secret.strip()
    if secret.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(secret)))
    return Keypair.from_base58_string(secret)


async def _rpc(http: httpx.AsyncClient, url: str, method: str, params: list[Any]) -> Any:
    try:
        response = await http.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20
        )
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise AttestError(f"rpc indisponível em {method}") from None
    if "error" in payload:
        # a mensagem do RPC pode citar a transação; não leva segredo
        raise AttestError(f"{method}: {str(payload['error'].get('message'))[:200]}")
    return payload.get("result")


async def submit_memo(http: httpx.AsyncClient, url: str, payer: Keypair, text: str) -> str:
    """Envia a transação de memo e devolve a assinatura, sem esperar a confirmação."""
    latest = await _rpc(http, url, "getLatestBlockhash", [{"commitment": "confirmed"}])
    blockhash = Hash.from_string(latest["value"]["blockhash"])
    instruction = Instruction(MEMO_PROGRAM, text.encode(), [])
    message = Message.new_with_blockhash([instruction], payer.pubkey(), blockhash)
    transaction = Transaction([payer], message, blockhash)
    signature = await _rpc(
        http,
        url,
        "sendTransaction",
        [
            base64.b64encode(bytes(transaction)).decode(),
            {"encoding": "base64", "preflightCommitment": "confirmed"},
        ],
    )
    if not isinstance(signature, str):
        raise AttestError("o RPC não devolveu a assinatura")
    return signature


async def confirmed_slot(http: httpx.AsyncClient, url: str, signature: str) -> int | None:
    """Slot da transação se já estiver confirmada; None se ainda não apareceu."""
    statuses = await _rpc(
        http, url, "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}]
    )
    status = (statuses or {}).get("value", [None])[0]
    if not status:
        return None
    if status.get("err") is not None:
        raise AttestError("a transação de memo falhou na rede")
    if status.get("confirmationStatus") in ("confirmed", "finalized"):
        return int(status["slot"])
    return None


async def wait_confirmation(http: httpx.AsyncClient, url: str, signature: str) -> int:
    for _ in range(CONFIRM_TIMEOUT // 2):
        await asyncio.sleep(2)
        slot = await confirmed_slot(http, url, signature)
        if slot is not None:
            return slot
    raise AttestError("a confirmação demorou demais")


async def memo_matches(http: httpx.AsyncClient, url: str, signature: str, text: str) -> bool | None:
    """True/False conforme o memo da transação contém `text`; None se não deu para consultar."""
    try:
        result = await _rpc(
            http,
            url,
            "getTransaction",
            [
                signature,
                {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"},
            ],
        )
    except AttestError:
        return None
    if result is None:
        return False
    if (result.get("meta") or {}).get("err") is not None:
        return False
    instructions = ((result.get("transaction") or {}).get("message") or {}).get("instructions") or []
    for instruction in instructions:
        if instruction.get("programId") == str(MEMO_PROGRAM) and instruction.get("parsed") == text:
            return True
    return False
