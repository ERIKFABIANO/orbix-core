from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from orbix.config import get_settings

# Supabase com chaves assimétricas. Lista fechada: nunca aceitar "none" nem HS256 aqui.
ALLOWED_ALGORITHMS = ["ES256", "RS256"]

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class User:
    id: UUID
    claims: dict[str, Any]


@lru_cache
def jwks_client() -> jwt.PyJWKClient:
    base = get_settings().supabase_base
    return jwt.PyJWKClient(f"{base}/auth/v1/.well-known/jwks.json", cache_keys=True)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="não autenticado",
        headers={"WWW-Authenticate": "Bearer"},
    )


def current_user(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    if cred is None:
        raise _unauthorized()
    base = get_settings().supabase_base
    try:
        key = jwks_client().get_signing_key_from_jwt(cred.credentials).key
        claims: dict[str, Any] = jwt.decode(
            cred.credentials,
            key,
            algorithms=ALLOWED_ALGORITHMS,
            audience="authenticated",
            issuer=f"{base}/auth/v1",
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
        user = User(id=UUID(claims["sub"]), claims=claims)
    except (jwt.PyJWTError, ValueError):
        raise _unauthorized() from None
    # usado pelo rate limit por usuário
    request.state.user_id = str(user.id)
    return user
