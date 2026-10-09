"""índice em events(ts) para a recotação da PTAX

A recotação (`prices.ptax.refresh_stale`) roda duas vezes por hora e filtra os eventos só
pela data (últimos 20 dias). Sem índice em `ts`, cada rodada varria a tabela inteira.

A leitura do extrato da Hyperliquid (depósitos, saques, transferências, airdrops) não precisa
de migração: o cursor antigo ("fills:funding") é aceito e o extrato é lido inteiro na próxima
sincronização de cada carteira.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("create index if not exists events_ts_idx on public.events (ts)")


def downgrade() -> None:
    op.execute("drop index if exists public.events_ts_idx")
