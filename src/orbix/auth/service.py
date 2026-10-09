"""Contas, formas de login e sessões. Tudo aqui roda em `Database.service()` (papel orbix_api)."""

from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID

import asyncpg

from orbix.config import Settings
from orbix.errors import AppError
from orbix.schemas import LoginMethod, Plan, UserOut
from orbix.security import new_token, sha256_bytes


async def _find_identity(conn: asyncpg.Connection, provider: str, subject: str) -> UUID | None:
    user_id = await conn.fetchval(
        "select user_id from orbix_auth.identities where provider = $1 and subject = $2",
        provider,
        subject,
    )
    return cast(UUID | None, user_id)


async def _find_by_email(conn: asyncpg.Connection, email: str) -> UUID | None:
    user_id = await conn.fetchval(
        "select user_id from orbix_auth.identities where email = $1 order by created_at limit 1",
        email,
    )
    return cast(UUID | None, user_id)


async def login_or_register(
    conn: asyncpg.Connection,
    *,
    provider: LoginMethod,
    subject: str,
    email: str | None = None,
    display_name: str | None = None,
    locale: str | None = None,
    link_to: UUID | None = None,
) -> UUID:
    """Devolve a conta dona desta forma de login, criando conta ou vínculo quando preciso.

    `email` só deve ser passado quando o provedor confirmou que o e-mail pertence à pessoa:
    contas com o mesmo e-mail confirmado viram uma só.
    """
    owner = await _find_identity(conn, provider, subject)
    if owner is not None:
        if link_to is not None and owner != link_to:
            raise AppError("identity_in_use", 409)
        await conn.execute(
            "update orbix_auth.identities set last_login_at = now() where provider = $1 and subject = $2",
            provider,
            subject,
        )
        return owner

    user_id = link_to
    if user_id is None and email:
        user_id = await _find_by_email(conn, email)
    created = user_id is None
    if user_id is None:
        # cria em auth.users; o trigger do banco cria a linha de profiles
        user_id = cast(UUID, await conn.fetchval("select orbix_auth.create_user()"))
        await conn.execute(
            "update public.profiles set display_name = $2, locale = $3 where id = $1",
            user_id,
            display_name[:60] if display_name else None,
            locale,
        )
    elif display_name:
        await conn.execute(
            "update public.profiles set display_name = $2 where id = $1 and display_name is null",
            user_id,
            display_name[:60],
        )

    try:
        async with conn.transaction():
            await conn.execute(
                "insert into orbix_auth.identities (user_id, provider, subject, email, last_login_at) "
                "values ($1, $2, $3, $4, now())",
                user_id,
                provider,
                subject,
                email,
            )
    except asyncpg.UniqueViolationError:
        # duas requisições simultâneas criando a mesma identidade
        if created:
            await conn.execute("select orbix_auth.delete_user($1)", user_id)
        owner = await _find_identity(conn, provider, subject)
        if owner is None or (link_to is not None and owner != link_to):
            raise AppError("identity_in_use", 409) from None
        return owner
    return user_id


async def ensure_login_wallet(conn: asyncpg.Connection, user_id: UUID, address: str) -> None:
    """Registra a carteira que assinou o login como verificada. A primeira vira a de login."""
    has_login = await conn.fetchval(
        "select exists(select 1 from public.wallets where user_id = $1 and is_login)", user_id
    )
    await conn.execute(
        """
        insert into public.wallets (user_id, chain, address, label, verified, verified_at, is_login)
        values ($1, 'solana', $2, 'Principal', true, now(), $3)
        on conflict (user_id, chain, address) do update
          set verified = true, verified_at = coalesce(public.wallets.verified_at, now())
        """,
        user_id,
        address,
        not has_login,
    )
    await conn.execute(
        "update public.profiles set primary_wallet = $2 where id = $1 and primary_wallet is null",
        user_id,
        address,
    )


async def create_session(
    conn: asyncpg.Connection,
    settings: Settings,
    user_id: UUID,
    method: LoginMethod,
    user_agent: str | None,
) -> tuple[str, datetime]:
    token = new_token()
    expires_at = datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)
    await conn.execute(
        "insert into orbix_auth.sessions (user_id, token_hash, method, expires_at, user_agent) "
        "values ($1, $2, $3, $4, $5)",
        user_id,
        sha256_bytes(token),
        method,
        expires_at,
        (user_agent or "")[:200] or None,
    )
    return token, expires_at


async def find_session(conn: asyncpg.Connection, token: str) -> tuple[UUID, UUID] | None:
    """(user_id, session_id) de um token válido, ou None."""
    row = await conn.fetchrow(
        "select id, user_id from orbix_auth.sessions "
        "where token_hash = $1 and revoked_at is null and expires_at > now()",
        sha256_bytes(token),
    )
    return (row["user_id"], row["id"]) if row else None


async def revoke_session(conn: asyncpg.Connection, session_id: UUID) -> None:
    await conn.execute(
        "update orbix_auth.sessions set revoked_at = now() where id = $1 and revoked_at is null",
        session_id,
    )


def _month_start(today: date) -> date:
    return today.replace(day=1)


def agent_quota(settings: Settings, plan: str) -> int:
    return settings.free_agent_questions if plan == "free" else settings.paid_agent_questions


async def load_user(conn: asyncpg.Connection, settings: Settings, user_id: UUID) -> UserOut | None:
    row = await conn.fetchrow(
        """
        select p.id, p.primary_wallet, p.display_name, p.plan, p.onboarded_at,
               p.agent_questions_used, p.agent_period,
               exists(select 1 from public.wallets w where w.user_id = p.id) as has_wallets,
               (select array_agg(distinct i.provider) from orbix_auth.identities i
                 where i.user_id = p.id) as methods,
               (select i.email from orbix_auth.identities i
                 where i.user_id = p.id and i.email is not null
                 order by i.created_at limit 1) as email
        from public.profiles p where p.id = $1
        """,
        user_id,
    )
    if row is None:
        return None
    # a cota do agente zera na virada do mês
    used = row["agent_questions_used"] if row["agent_period"] == _month_start(date.today()) else 0
    return UserOut(
        id=str(row["id"]),
        address=row["primary_wallet"],
        email=row["email"],
        display_name=row["display_name"],
        login_methods=sorted(row["methods"] or []),
        has_wallets=row["has_wallets"],
        plan=cast(Plan, row["plan"]),
        agent_questions_left=max(0, agent_quota(settings, row["plan"]) - used),
        onboarded=row["onboarded_at"] is not None,
    )


async def consume_agent_question(conn: asyncpg.Connection, settings: Settings, user_id: UUID) -> int:
    """Gasta uma pergunta do mês e devolve quantas restam. Levanta `quota` se acabou.

    A checagem e o desconto são um único UPDATE: duas perguntas ao mesmo tempo com uma
    restante não passam as duas. Quem chama desconta antes de chamar o modelo e devolve
    com `refund_agent_question` se o modelo falhar."""
    period = _month_start(date.today())
    row = await conn.fetchrow(
        """
        update public.profiles
           set agent_questions_used = case when agent_period = $2 then agent_questions_used else 0 end + 1,
               agent_period = $2
         where id = $1
           and case when agent_period = $2 then agent_questions_used else 0 end
               < case when plan = 'free' then $3::int else $4::int end
        returning plan, agent_questions_used
        """,
        user_id,
        period,
        settings.free_agent_questions,
        settings.paid_agent_questions,
    )
    if row is None:
        exists = await conn.fetchval("select exists(select 1 from public.profiles where id = $1)", user_id)
        raise AppError("quota", 429) if exists else AppError("unauthorized", 401)
    return int(agent_quota(settings, row["plan"]) - row["agent_questions_used"])


async def refund_agent_question(conn: asyncpg.Connection, settings: Settings, user_id: UUID) -> int:
    """Devolve a pergunta descontada quando o modelo não respondeu. Devolve quantas restam."""
    row = await conn.fetchrow(
        """
        update public.profiles
           set agent_questions_used = greatest(agent_questions_used - 1, 0)
         where id = $1 and agent_period = $2
        returning plan, agent_questions_used
        """,
        user_id,
        _month_start(date.today()),
    )
    if row is None:
        return 0
    return max(0, int(agent_quota(settings, row["plan"]) - row["agent_questions_used"]))


async def delete_account(conn: asyncpg.Connection, user_id: UUID) -> None:
    # cascata: carteiras, eventos, relatórios, histórico do agente, identidades e sessões
    await conn.execute("select orbix_auth.delete_user($1)", user_id)
