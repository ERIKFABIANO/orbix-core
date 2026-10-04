from typing import cast

from fastapi import APIRouter, Depends, Request, Response

from orbix.auth import service as auth_service
from orbix.config import Settings
from orbix.db import Database
from orbix.deps import CurrentUser, current_user, get_db, settings_dep
from orbix.errors import unauthorized
from orbix.ratelimit import limiter
from orbix.schemas import UserOut
from orbix.storage import Storage, user_prefix

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me", response_model=UserOut)
async def me(
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> UserOut:
    async with db.service() as conn:
        found = await auth_service.load_user(conn, settings, user.id)
    if found is None:
        raise unauthorized()
    return found


@router.delete("/me", status_code=204)
@limiter.limit("5/hour")
async def delete_me(
    request: Request,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
) -> Response:
    """Exclusão definitiva (LGPD, art. 18). Os hashes já gravados na Solana permanecem."""
    async with db.service() as conn:
        await auth_service.delete_account(conn, user.id)
    storage = cast(Storage | None, request.app.state.storage)
    if storage is not None:
        await storage.delete_prefix(user_prefix(user.id))
    return Response(status_code=204)
