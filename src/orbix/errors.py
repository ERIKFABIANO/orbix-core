from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from orbix.i18n import error_message

log = structlog.get_logger()


class AppError(Exception):
    """Erro esperado, devolvido no formato do contrato: { error: { code, message } }."""

    def __init__(self, code: str, status: int = 400, **params: object) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.params = params


def unauthorized() -> AppError:
    return AppError("unauthorized", 401)


def not_found() -> AppError:
    return AppError("not_found", 404)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def error_body(request: Request, code: str, **params: object) -> dict[str, Any]:
    return {
        "error": {"code": code, "message": error_message(code, **params)},
        "request_id": _request_id(request),
    }


_STATUS_CODES = {401: "unauthorized", 404: "not_found", 405: "not_found", 422: "invalid_request"}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            error_body(request, exc.code, **exc.params), status_code=exc.status, headers=headers
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "invalid_request")
        return JSONResponse(error_body(request, code), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # devolve só onde está o problema, sem ecoar o valor enviado
        body = error_body(request, "invalid_request")
        body["error"]["fields"] = [".".join(str(p) for p in e.get("loc", [])[1:]) for e in exc.errors()]
        return JSONResponse(body, status_code=422)

    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(error_body(request, "rate_limited", seconds=60), status_code=429)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # detalhe só no log do servidor; o cliente recebe só o request_id
        log.exception("erro não tratado")
        return JSONResponse(error_body(request, "internal"), status_code=500)
