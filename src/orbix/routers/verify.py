import re
from typing import cast

import httpx
from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis

from orbix.attest.memo import memo_matches, memo_text
from orbix.config import Settings
from orbix.db import Database
from orbix.deps import get_db, get_redis, settings_dep
from orbix.errors import not_found
from orbix.i18n import get_locale
from orbix.ratelimit import limiter
from orbix.schemas import PublicVerificationOut

router = APIRouter(prefix="/api/verify", tags=["verify"])

PUBLIC_ID = re.compile(r"^[0-9a-f]{8,64}$")
MONTHS = {
    "pt": [
        "Janeiro",
        "Fevereiro",
        "Março",
        "Abril",
        "Maio",
        "Junho",
        "Julho",
        "Agosto",
        "Setembro",
        "Outubro",
        "Novembro",
        "Dezembro",
    ],
    "en": [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ],
}
CACHE_TTL = 3600


@router.get("/{public_id}", response_model=PublicVerificationOut)
@limiter.limit("60/minute")
async def verify_public(
    request: Request,
    public_id: str,
    db: Database = Depends(get_db),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(settings_dep),
) -> PublicVerificationOut:
    """Rota pública. Devolve só mês, hash e dados da transação: nada que identifique o titular."""
    public_id = public_id.lower()
    if not PUBLIC_ID.fullmatch(public_id):
        raise not_found()
    async with db.service() as conn:
        row = await conn.fetchrow(
            "select month, sha256, solana_sig, slot, attested_at from public.reports "
            "where public_id = $1 and status = 'final' and solana_sig is not null",
            public_id,
        )
    if row is None:
        raise not_found()

    cache_key = f"verify:{row['solana_sig']}"
    cached = await redis.get(cache_key)
    if cached is not None:
        valid = cached == b"1"
    else:
        http = cast(httpx.AsyncClient, request.app.state.http)
        checked = await memo_matches(
            http,
            settings.solana_memo_rpc_url.get_secret_value(),
            row["solana_sig"],
            memo_text(row["sha256"]),
        )
        # sem resposta do RPC, vale o que o worker confirmou ao gravar a transação
        valid = True if checked is None else checked
        if checked is not None:
            await redis.set(cache_key, b"1" if valid else b"0", ex=CACHE_TTL)

    month = row["month"]
    locale = get_locale()
    period = f"{MONTHS[locale][month.month - 1]}/{month.year}"
    description = (
        f"Monthly report · {period} · holder hidden"
        if locale == "en"
        else f"Relatório mensal · {period} · titular ocultado"
    )
    return PublicVerificationOut(
        public_id=public_id,
        description=description,
        month=month.strftime("%Y-%m"),
        hash=row["sha256"],
        tx_signature=row["solana_sig"],
        slot=row["slot"],
        registered_at=row["attested_at"],
        valid=valid,
    )
