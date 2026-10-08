"""releitura das carteiras Solana para gravar a contraparte das transferências

A leitura passou a guardar no `raw` o endereço do outro lado de cada transferência. A
releitura normal para na transação mais nova já conhecida, então as transferências antigas
ficariam sem contraparte. Zerar o cursor faz a próxima sincronização reler a carteira; os
eventos que já existem só recebem o campo novo no `raw` (preço e custo manuais não mudam).

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("update public.wallets set sync_cursor = null where chain = 'solana'")


def downgrade() -> None:
    # nada a desfazer: o cursor volta sozinho na próxima sincronização
    pass
