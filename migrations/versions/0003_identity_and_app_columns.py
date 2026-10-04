"""login próprio do back-end (carteira, e-mail, Google, GitHub), sessões e colunas do contrato

Parte do schema de 01/10 (com event_reviews, ingestion_runs, report_versions e os triggers de
proteção). Nada do que já existe é alterado: só entram tabelas, colunas, funções e permissões.

O usuário continua existindo em `auth.users`, como todas as chaves estrangeiras esperam, mas
quem cria é o back-end, pela função `orbix_auth.create_user()`. Isso dispensa a chave
`service_role` dentro da API. O RLS continua igual: a API define `request.jwt.claims` com o
id do usuário a cada requisição, e `auth.uid()` lê dali.

- orbix_auth.identities: formas de login de cada conta.
- orbix_auth.sessions: sessões por token opaco (guardado só como SHA-256).
- wallets / profiles / reports: colunas de status, cota do agente e atestação.
- public.agent_messages: histórico do agente, com RLS.
- políticas e grants dos papéis orbix_api e orbix_worker (as do worker tinham sido removidas).

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

API_TABLES = ("profiles", "wallets", "reports", "report_versions")
WORKER_TABLES = (
    "wallets",
    "profiles",
    "events",
    "prices",
    "fx_rates",
    "assets",
    "reports",
    "report_versions",
)


def upgrade() -> None:
    # ── colunas novas (aditivas) ───────────────────────────────────────
    op.execute("""
        alter table public.profiles
          add column if not exists onboarded_at timestamptz,
          add column if not exists agent_questions_used integer not null default 0,
          add column if not exists agent_period date,
          add column if not exists locale text,
          add constraint profiles_locale_check check (locale is null or locale in ('pt', 'en')),
          add constraint profiles_agent_used_check check (agent_questions_used >= 0)
    """)
    op.execute("""
        alter table public.wallets
          add column if not exists is_login boolean not null default false,
          add column if not exists verified_at timestamptz,
          add column if not exists status text not null default 'pending',
          add column if not exists sync_error text,
          add column if not exists sync_read integer not null default 0,
          add column if not exists sync_total integer,
          add column if not exists sync_cursor text,
          add column if not exists sync_started_at timestamptz,
          add constraint wallets_status_check
            check (status in ('pending', 'syncing', 'synced', 'empty', 'error')),
          add constraint wallets_sync_error_check check (char_length(sync_error) <= 300)
    """)
    op.execute("create unique index wallets_one_login_idx on public.wallets (user_id) where is_login")
    op.execute("""
        alter table public.reports
          add column if not exists public_id text,
          add column if not exists cost_brl numeric not null default 0,
          add column if not exists tax_brl numeric not null default 0,
          add column if not exists events_count integer not null default 0,
          add column if not exists finalized_at timestamptz,
          add column if not exists slot bigint,
          add column if not exists attested_at timestamptz,
          add column if not exists attest_error text,
          add column if not exists decripto_key text,
          add constraint reports_public_id_check check (public_id ~ '^[0-9a-f]{8,64}$')
    """)
    op.execute("create unique index reports_public_id_idx on public.reports (public_id)")

    # ── schema privado de identidade ───────────────────────────────────
    op.execute("create schema if not exists orbix_auth")
    op.execute("revoke all on schema orbix_auth from public, anon, authenticated")
    op.execute("grant usage on schema orbix_auth to orbix_api")

    # O back-end cria o usuário em auth.users sem a chave service_role: a função roda com
    # os direitos do dono (postgres) e só o papel da API pode chamá-la.
    op.execute("""
        create function orbix_auth.create_user() returns uuid
          language plpgsql security definer set search_path = '' as $$
        declare
          new_id uuid := gen_random_uuid();
        begin
          insert into auth.users
            (id, instance_id, aud, role, created_at, updated_at, raw_app_meta_data, raw_user_meta_data)
          values
            (new_id, '00000000-0000-0000-0000-000000000000', 'authenticated', 'authenticated',
             now(), now(), '{"provider": "orbix", "providers": ["orbix"]}'::jsonb, '{}'::jsonb);
          return new_id;
        end
        $$
    """)
    op.execute("""
        create function orbix_auth.delete_user(target uuid) returns void
          language plpgsql security definer set search_path = '' as $$
        begin
          -- cascata: profile, carteiras, eventos, relatórios, identidades e sessões
          delete from auth.users where id = target;
        end
        $$
    """)
    for function in ("create_user()", "delete_user(uuid)"):
        op.execute(f"revoke all on function orbix_auth.{function} from public, anon, authenticated")
        op.execute(f"grant execute on function orbix_auth.{function} to orbix_api")

    op.execute("""
        create table orbix_auth.identities (
          id uuid primary key default gen_random_uuid(),
          user_id uuid not null references auth.users (id) on delete cascade,
          provider text not null check (provider in ('wallet', 'email', 'google', 'github')),
          -- endereço da carteira, e-mail em minúsculas ou id do usuário no provedor
          subject text not null check (char_length(subject) between 1 and 320),
          -- e-mail confirmado pelo provedor; usado para unir contas com o mesmo e-mail
          email text check (email = lower(email) and char_length(email) <= 320),
          created_at timestamptz not null default now(),
          last_login_at timestamptz,
          unique (provider, subject)
        )
    """)
    op.execute("create index identities_user_idx on orbix_auth.identities (user_id)")
    op.execute("create index identities_email_idx on orbix_auth.identities (email) where email is not null")
    op.execute("""
        create table orbix_auth.sessions (
          id uuid primary key default gen_random_uuid(),
          user_id uuid not null references auth.users (id) on delete cascade,
          -- o token em si nunca é guardado, só o SHA-256
          token_hash bytea not null unique check (octet_length(token_hash) = 32),
          method text not null check (method in ('wallet', 'email', 'google', 'github')),
          created_at timestamptz not null default now(),
          expires_at timestamptz not null,
          revoked_at timestamptz,
          last_seen_at timestamptz,
          user_agent text check (char_length(user_agent) <= 200)
        )
    """)
    op.execute("create index sessions_user_idx on orbix_auth.sessions (user_id)")
    op.execute("create index sessions_expires_idx on orbix_auth.sessions (expires_at)")
    op.execute(
        "grant select, insert, update, delete on orbix_auth.identities, orbix_auth.sessions to orbix_api"
    )

    # ── histórico do agente (dado do usuário, protegido por RLS) ───────
    op.execute("""
        create table public.agent_messages (
          id uuid primary key default gen_random_uuid(),
          user_id uuid not null default auth.uid() references auth.users (id) on delete cascade,
          conversation_id uuid not null,
          role text not null check (role in ('user', 'assistant')),
          content jsonb not null,
          month date,
          created_at timestamptz not null default now()
        )
    """)
    op.execute(
        "create index agent_messages_conv_idx on public.agent_messages (user_id, conversation_id, created_at)"
    )
    op.execute("alter table public.agent_messages enable row level security")
    # o Supabase concede tudo a anon/authenticated em tabela nova de public: tirar
    op.execute("revoke all on public.agent_messages from anon, authenticated")
    op.execute("grant select, insert on public.agent_messages to authenticated")
    op.execute("""
        create policy orbix_read on public.agent_messages
          for select to authenticated using (user_id = (select auth.uid()))
    """)
    op.execute("""
        create policy orbix_insert on public.agent_messages
          for insert to authenticated with check (user_id = (select auth.uid()))
    """)

    # ── API: identidade e operações de sistema (sempre com user_id explícito no SQL) ──
    op.execute("grant usage on schema public to orbix_api")
    op.execute("grant select on public.profiles to orbix_api")
    op.execute("""
        grant update (primary_wallet, display_name, onboarded_at, agent_questions_used,
                      agent_period, locale)
          on public.profiles to orbix_api
    """)
    op.execute("grant select, insert, delete on public.wallets to orbix_api")
    op.execute("""
        grant update (label, verified, verified_at, is_login, status, sync_error, sync_read,
                      sync_total, sync_cursor, sync_started_at)
          on public.wallets to orbix_api
    """)
    op.execute("grant select, insert, update on public.reports to orbix_api")
    op.execute("grant select on public.report_versions to orbix_api")
    for table in API_TABLES:
        op.execute(f"""
            create policy orbix_api_service on public.{table}
              for all to orbix_api using (true) with check (true)
        """)

    # ── usuário: preço manual nos próprios eventos (o trigger de auditoria registra) ──
    op.execute("""
        grant update (usd_price, brl_value, ptax, ptax_date, price_ts, pricing_policy,
                      review_status, review_reason)
          on public.events to authenticated
    """)
    op.execute("""
        create policy orbix_event_price_update on public.events
          for update to authenticated
          using (user_id = (select auth.uid())) with check (user_id = (select auth.uid()))
    """)

    # ── worker: grants mínimos (sem DELETE) e políticas, que tinham sido removidas ──
    op.execute("grant usage on schema public to orbix_worker")
    op.execute("grant select on public.wallets, public.profiles to orbix_worker")
    op.execute("""
        grant update (last_synced_at, verified, status, sync_error, sync_read, sync_total,
                      sync_cursor, sync_started_at)
          on public.wallets to orbix_worker
    """)
    op.execute("grant update (onboarded_at) on public.profiles to orbix_worker")
    op.execute(
        "grant select, insert, update on public.events, public.prices, public.fx_rates, "
        "public.assets, public.reports to orbix_worker"
    )
    op.execute("grant select, insert on public.report_versions to orbix_worker")
    for table in WORKER_TABLES:
        op.execute(f'drop policy if exists "worker: acesso de serviço" on public.{table}')
        op.execute(f"""
            create policy orbix_worker_service on public.{table}
              for all to orbix_worker using (true) with check (true)
        """)


def downgrade() -> None:
    for table in WORKER_TABLES:
        op.execute(f"drop policy if exists orbix_worker_service on public.{table}")
    op.execute("revoke insert on public.report_versions from orbix_worker")
    op.execute("revoke update (onboarded_at) on public.profiles from orbix_worker")
    op.execute("""
        revoke update (status, sync_error, sync_read, sync_total, sync_cursor, sync_started_at)
          on public.wallets from orbix_worker
    """)
    op.execute("drop policy if exists orbix_event_price_update on public.events")
    op.execute("""
        revoke update (usd_price, brl_value, ptax, ptax_date, price_ts, pricing_policy,
                       review_status, review_reason)
          on public.events from authenticated
    """)
    for table in API_TABLES:
        op.execute(f"drop policy if exists orbix_api_service on public.{table}")
        op.execute(f"revoke all on public.{table} from orbix_api")
    op.execute("revoke usage on schema public from orbix_api")
    op.execute("drop table if exists public.agent_messages")
    # apaga também as contas criadas pelo back-end: sem identidades, elas ficariam órfãs
    op.execute("delete from auth.users where raw_app_meta_data ->> 'provider' = 'orbix'")
    op.execute("drop schema if exists orbix_auth cascade")
    op.execute("drop index if exists public.reports_public_id_idx")
    op.execute("""
        alter table public.reports
          drop constraint if exists reports_public_id_check,
          drop column if exists public_id, drop column if exists cost_brl,
          drop column if exists tax_brl, drop column if exists events_count,
          drop column if exists finalized_at, drop column if exists slot,
          drop column if exists attested_at, drop column if exists attest_error,
          drop column if exists decripto_key
    """)
    op.execute("drop index if exists public.wallets_one_login_idx")
    op.execute("""
        alter table public.wallets
          drop constraint if exists wallets_status_check,
          drop constraint if exists wallets_sync_error_check,
          drop column if exists is_login, drop column if exists verified_at,
          drop column if exists status, drop column if exists sync_error,
          drop column if exists sync_read, drop column if exists sync_total,
          drop column if exists sync_cursor, drop column if exists sync_started_at
    """)
    op.execute("""
        alter table public.profiles
          drop constraint if exists profiles_locale_check,
          drop constraint if exists profiles_agent_used_check,
          drop column if exists onboarded_at, drop column if exists agent_questions_used,
          drop column if exists agent_period, drop column if exists locale
    """)
