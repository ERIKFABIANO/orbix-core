"""papéis da aplicação, grants mínimos, integridade e salt do hash

- orbix_api: API. Sem BYPASSRLS. Cada requisição faz SET LOCAL ROLE authenticated.
- orbix_worker: jobs (ingestão, preços, relatórios). Sem BYPASSRLS; só escreve onde
  há política explícita para ele, e nunca apaga.
- GRANT por coluna: usuário não consegue mudar o próprio plano nem marcar carteira
  como verificada.
- FK composta garante que events.user_id é o dono da carteira.
- reports.salt para o hash com salt gravado na Solana.

Senhas dos papéis NÃO ficam aqui: definir depois com
  alter role orbix_api password '...';   (gerar com secrets.token_urlsafe(32))

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── papéis ─────────────────────────────────────────────────────────
    op.execute("""
        do $$ begin
          if not exists (select 1 from pg_roles where rolname = 'orbix_api') then
            create role orbix_api login nobypassrls noinherit nocreatedb nocreaterole;
          end if;
          if not exists (select 1 from pg_roles where rolname = 'orbix_worker') then
            create role orbix_worker login nobypassrls nocreatedb nocreaterole;
          end if;
        end $$;
    """)
    # a API só age como usuário: precisa poder assumir authenticated
    op.execute("grant authenticated to orbix_api")
    op.execute("alter role orbix_api set statement_timeout = '15s'")
    op.execute("alter role orbix_worker set statement_timeout = '120s'")
    op.execute("grant usage on schema public to orbix_worker")

    # ── grants do usuário (authenticated): só as colunas que ele pode mexer ──
    op.execute("revoke update on public.profiles from authenticated")
    op.execute("grant update (display_name) on public.profiles to authenticated")
    op.execute("revoke insert, update on public.wallets from authenticated")
    op.execute("grant insert (chain, address, label) on public.wallets to authenticated")
    op.execute("grant update (label) on public.wallets to authenticated")

    # ── worker: grants mínimos, sem DELETE em lugar nenhum ─────────────
    op.execute("grant select on public.wallets, public.profiles to orbix_worker")
    op.execute("grant update (last_synced_at, verified) on public.wallets to orbix_worker")
    op.execute("grant select, insert, update on public.events to orbix_worker")
    op.execute(
        "grant select, insert, update on public.prices, public.fx_rates, public.assets to orbix_worker"
    )
    op.execute("grant select, insert, update on public.reports to orbix_worker")

    for table in ("wallets", "profiles", "events", "prices", "fx_rates", "assets", "reports"):
        op.execute(f"""
            create policy "worker: acesso de serviço" on public.{table}
              for all to orbix_worker using (true) with check (true)
        """)

    # ── integridade: evento sempre pertence ao dono da carteira ────────
    op.execute("alter table public.wallets add constraint wallets_id_user_id_key unique (id, user_id)")
    op.execute("""
        alter table public.events
          add constraint events_wallet_owner_fkey
          foreign key (wallet_id, user_id) references public.wallets (id, user_id)
          on delete cascade
    """)

    # ── hash do relatório com salt ─────────────────────────────────────
    op.execute("alter table public.reports add column if not exists salt bytea")
    op.execute("""
        alter table public.reports add constraint reports_salt_len_check
          check (salt is null or octet_length(salt) = 32)
    """)
    # o salt nunca sai para o front: authenticated lê todas as colunas, menos salt
    op.execute("revoke select on public.reports from authenticated")
    op.execute("""
        grant select (id, user_id, month, status, total_brl, gains_brl, over_35k, data,
                      file_url, sha256, solana_sig, created_at, updated_at)
          on public.reports to authenticated
    """)


def downgrade() -> None:
    op.execute("revoke select on public.reports from authenticated")
    op.execute("grant select on public.reports to authenticated")
    op.execute("alter table public.reports drop constraint if exists reports_salt_len_check")
    op.execute("alter table public.reports drop column if exists salt")
    op.execute("alter table public.events drop constraint if exists events_wallet_owner_fkey")
    op.execute("alter table public.wallets drop constraint if exists wallets_id_user_id_key")
    for table in ("wallets", "profiles", "events", "prices", "fx_rates", "assets", "reports"):
        op.execute(f'drop policy if exists "worker: acesso de serviço" on public.{table}')
    op.execute("revoke all on all tables in schema public from orbix_worker")
    op.execute("revoke usage on schema public from orbix_worker")
    op.execute("revoke update (label) on public.wallets from authenticated")
    op.execute("revoke insert (chain, address, label) on public.wallets from authenticated")
    op.execute("revoke update (display_name) on public.profiles from authenticated")
    op.execute("revoke authenticated from orbix_api")
    op.execute("drop role if exists orbix_worker")
    op.execute("drop role if exists orbix_api")
