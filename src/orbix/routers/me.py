from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from orbix.auth import User, current_user
from orbix.ratelimit import limiter

router = APIRouter(tags=["me"])


class MeOut(BaseModel):
    id: UUID


@router.get("/me", response_model=MeOut)
@limiter.limit("30/minute")
async def me(request: Request, user: User = Depends(current_user)) -> MeOut:
    return MeOut(id=user.id)
