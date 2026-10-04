"""evidência nas revisões de preço e registro das leituras em ingestion_runs

- A API grava a evidência que o usuário informa ao corrigir um preço. A linha de
  event_reviews em si continua sendo criada pelo trigger do banco.
- O worker registra cada leitura de carteira em ingestion_runs (período coberto,
  situação e limitações), que alimenta a cobertura mostrada no relatório.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("grant select, update (evidence) on public.event_reviews to orbix_api")
    op.execute("""
        create policy orbix_api_service on public.event_reviews
          for all to orbix_api using (true) with check (true)
    """)
    op.execute("grant select on public.ingestion_runs to orbix_api")
    op.execute("""
        grant select, insert,
              update (status, covered_from, covered_to, cursor_data, limitations, finished_at)
          on public.ingestion_runs to orbix_worker
    """)
    op.execute("""
        create policy orbix_worker_service on public.ingestion_runs
          for all to orbix_worker using (true) with check (true)
    """)
    op.execute("""
        create policy orbix_api_service on public.ingestion_runs
          for select to orbix_api using (true)
    """)
    # a recotação periódica procura eventos sem valor; sem índice, varre a tabela inteira
    op.execute(
        "create index if not exists events_unvalued_idx on public.events (user_id) where brl_value is null"
    )


def downgrade() -> None:
    op.execute("drop index if exists public.events_unvalued_idx")
    op.execute("drop policy if exists orbix_api_service on public.ingestion_runs")
    op.execute("drop policy if exists orbix_worker_service on public.ingestion_runs")
    op.execute("revoke all on public.ingestion_runs from orbix_worker, orbix_api")
    op.execute("drop policy if exists orbix_api_service on public.event_reviews")
    op.execute("revoke all on public.event_reviews from orbix_api")
