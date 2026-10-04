"""custo de aquisição informado pelo usuário quando a compra não está no histórico lido

Fica no evento de saída do swap (o que identifica a linha do relatório). A alteração passa
pelo mesmo trigger de auditoria do preço manual, com motivo e evidência em event_reviews.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        alter table public.events
          add column if not exists cost_override_brl numeric,
          add constraint events_cost_override_check
            check (cost_override_brl is null or (cost_override_brl >= 0 and cost_override_brl <> 'NaN'))
    """)
    op.execute("grant update (cost_override_brl) on public.events to authenticated")


def downgrade() -> None:
    op.execute("revoke update (cost_override_brl) on public.events from authenticated")
    op.execute("""
        alter table public.events
          drop constraint if exists events_cost_override_check,
          drop column if exists cost_override_brl
    """)
