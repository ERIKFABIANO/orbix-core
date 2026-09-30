import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

log = structlog.get_logger()


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"error": exc.detail, "request_id": _request_id(request)},
            status_code=exc.status_code,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # devolve só onde e o quê, sem ecoar o valor enviado
        fields = [
            {"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()
        ]
        return JSONResponse(
            {"error": "entrada inválida", "fields": fields, "request_id": _request_id(request)},
            status_code=422,
        )

    @app.exception_handler(RateLimitExceeded)
    async def rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
        return JSONResponse(
            {"error": "muitas requisições", "request_id": _request_id(request)},
            status_code=429,
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        # detalhe só no log do servidor; o cliente recebe só o request_id
        log.exception("erro não tratado")
        return JSONResponse(
            {"error": "erro interno", "request_id": _request_id(request)}, status_code=500
        )
