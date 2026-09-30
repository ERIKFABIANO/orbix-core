import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

# endereços Solana (base58) e EVM no meio de texto: mascarar antes de logar
_SOLANA = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_EVM = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
_SENSITIVE_KEYS = {"authorization", "token", "api_key", "secret", "password", "cookie"}


def mask_address(value: str) -> str:
    return f"{value[:4]}…{value[-4:]}" if len(value) > 10 else "…"


def _scrub(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key, value in list(event.items()):
        if any(s in key.lower() for s in _SENSITIVE_KEYS):
            event[key] = "[removido]"
        elif isinstance(value, str):
            value = _EVM.sub(lambda m: mask_address(m.group()), value)
            event[key] = _SOLANA.sub(lambda m: mask_address(m.group()), value)
    return event


def configure_logging(json_logs: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    renderer: Any = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            _scrub,
            renderer,
        ],
    )
