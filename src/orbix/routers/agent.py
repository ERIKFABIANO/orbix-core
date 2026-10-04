from typing import cast

from fastapi import APIRouter, Depends, Request

from orbix.agent import service as agent
from orbix.agent.llm import AgentLLM
from orbix.auth import service as auth_service
from orbix.config import Settings
from orbix.db import Database
from orbix.deps import CurrentUser, current_user, get_db, settings_dep
from orbix.errors import AppError
from orbix.ratelimit import limiter
from orbix.routers.dashboard import _previous
from orbix.schemas import AgentIn, AgentMessageOut, AgentReplyOut
from orbix.tax import service as tax

router = APIRouter(prefix="/api", tags=["agent"])


@router.post("/agent", response_model=AgentReplyOut)
@limiter.limit("10/minute")
async def ask_agent(
    request: Request,
    body: AgentIn,
    user: CurrentUser = Depends(current_user),
    db: Database = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> AgentReplyOut:
    llm = cast(AgentLLM | None, request.app.state.llm)
    if llm is None:
        raise AppError("agent_unavailable", 503)

    async with db.service() as conn:
        profile = await auth_service.load_user(conn, settings, user.id)
    if profile is None:
        raise AppError("unauthorized", 401)
    if profile.agent_questions_left <= 0:
        raise AppError("quota", 429)

    conversation = agent.conversation_uuid(body.conversation_id)
    async with db.as_user(user.id) as conn:
        rows = await tax.load_rows(conn)
        available = tax.months_with_rows(rows)
        month = body.month or (available[0] if available else tax.current_month())
        final = await conn.fetchval(
            "select exists(select 1 from public.reports where status = 'final' "
            "and month = to_date($1, 'YYYY-MM'))",
            month,
        )
        history = await agent.load_history(conn, conversation)

    status = "final" if final else "draft"
    month_rows = tax.rows_of(rows, month)
    totals = tax.totals_of(month_rows, settings)
    previous = tax.totals_of(tax.rows_of(rows, _previous(month)), settings)
    system, user_message, refs = agent.build_prompt(
        settings, month, status, tax.report_rows(month_rows), totals, previous, body.message
    )
    answer = await agent.ask(llm, system, history, user_message)
    blocks, source = agent.build_blocks(answer, refs, status, month)

    # a pergunta só é descontada da cota depois que o modelo respondeu
    async with db.service() as conn:
        left = await auth_service.consume_agent_question(conn, settings, user.id)
    async with db.as_user(user.id) as conn:
        message_id, created_at = await agent.save_exchange(
            conn, conversation, month, body.message, blocks, answer.text
        )

    return AgentReplyOut(
        conversation_id=str(conversation),
        message=AgentMessageOut(id=str(message_id), role="assistant", blocks=blocks, created_at=created_at),
        context=agent.build_context(settings, month, status, totals, source),
        suggestions=[s[:120] for s in answer.suggestions[:3]],
        questions_left=left,
    )
