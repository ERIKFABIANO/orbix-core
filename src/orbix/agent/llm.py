"""Chamada ao modelo de linguagem. O modelo devolve um objeto estruturado, nunca HTML ou links."""

import time
from typing import Any, Literal, Protocol

import structlog
from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from orbix.errors import AppError

log = structlog.get_logger()


class BreakdownLine(BaseModel):
    label: str
    value: str
    emphasis: Literal["none", "total", "gain"]


class AgentAnswer(BaseModel):
    # sem limites no schema: a saída estruturada do modelo não aceita todos; ver `clamp`
    text: str
    breakdown: list[BreakdownLine]
    # referências às linhas fornecidas em <dados> (ex.: "r3"); o servidor monta os links
    cited_rows: list[str]
    suggestions: list[str]

    def clamp(self) -> "AgentAnswer":
        """Corta a resposta em tamanhos seguros antes de guardar e devolver."""
        return AgentAnswer(
            text=self.text[:2500],
            breakdown=[
                BreakdownLine(label=line.label[:80], value=line.value[:40], emphasis=line.emphasis)
                for line in self.breakdown[:8]
            ],
            cited_rows=[ref[:8] for ref in self.cited_rows[:6]],
            suggestions=[s[:120] for s in self.suggestions[:3]],
        )


class AgentLLM(Protocol):
    async def answer(self, system: str, messages: list[dict[str, str]]) -> AgentAnswer: ...


class OpenAIAgent:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key, timeout=45, max_retries=1)
        self._model = model

    async def answer(self, system: str, messages: list[dict[str, str]]) -> AgentAnswer:
        payload: list[Any] = [{"role": "system", "content": system}, *messages]
        started = time.monotonic()
        try:
            completion = await self._client.chat.completions.parse(
                model=self._model,
                messages=payload,
                response_format=AgentAnswer,
                max_completion_tokens=1200,
            )
        except (OpenAIError, ValidationError) as exc:
            # OpenAIError cobre também a resposta cortada por tamanho e a barrada pelo filtro de
            # conteúdo, que não são APIError: antes viravam erro 500 em vez da resposta por regras
            log.error("agente: falha na chamada ao modelo", kind=type(exc).__name__)
            raise AppError("agent_unavailable", 503) from None
        # custo e tempo por pergunta, sem o conteúdo: serve para escolher modelo e plano
        usage = completion.usage
        log.info(
            "agente: resposta do modelo",
            model=self._model,
            seconds=round(time.monotonic() - started, 2),
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
        )
        parsed = completion.choices[0].message.parsed if completion.choices else None
        if parsed is None:
            raise AppError("agent_unavailable", 503)
        return parsed.clamp()

    async def close(self) -> None:
        await self._client.close()
