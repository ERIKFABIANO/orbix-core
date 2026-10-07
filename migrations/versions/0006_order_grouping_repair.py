"""reparo dos fills da Hyperliquid gravados com o hash zerado e mês em Brasília na view

Antes da correção de 06/10, fills sem hash de verdade (a Hyperliquid zera o hash de parte
deles) eram gravados todos com a mesma chave e viravam um único evento com vários ativos
misturados. A leitura nova já grava certo, mas não regrava o que estava no banco: a
releitura parte do cursor e não volta atrás.

Esta migração apaga só esses eventos e zera o cursor das carteiras Hyperliquid. Na próxima
sincronização a carteira é lida desde o início: os eventos apagados voltam com a chave
certa e os demais só recebem o par e a ordem no `raw` (preço e custo manuais ficam como estão).

A view monthly_summary agrupava o mês em UTC; passa a usar o horário de Brasília, como o
resto do sistema.

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

ZERO_HASH = "0x" + "0" * 64


def upgrade() -> None:
    op.execute(f"delete from public.events where chain = 'hyperliquid' and tx_hash = '{ZERO_HASH}'")  # noqa: S608
    op.execute("update public.wallets set sync_cursor = null where chain = 'hyperliquid'")
    op.execute("""
        create or replace view public.monthly_summary with (security_invoker = true) as
        select user_id,
               date_trunc('month', ts at time zone 'America/Sao_Paulo')::date as month,
               count(*) as events,
               count(*) filter (where brl_value is null) as unpriced_events,
               coalesce(sum(abs(brl_value)), 0) as volume_brl
          from public.events
         group by user_id, date_trunc('month', ts at time zone 'America/Sao_Paulo')
    """)


def downgrade() -> None:
    # os eventos apagados voltam sozinhos na próxima sincronização; só a view tem volta
    op.execute("""
        create or replace view public.monthly_summary with (security_invoker = true) as
        select user_id,
               date_trunc('month', ts)::date as month,
               count(*) as events,
               count(*) filter (where brl_value is null) as unpriced_events,
               coalesce(sum(abs(brl_value)), 0) as volume_brl
          from public.events
         group by user_id, date_trunc('month', ts)
    """)
