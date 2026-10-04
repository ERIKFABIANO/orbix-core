"""Fila de tarefas (arq sobre Redis). A API só enfileira; quem executa é o worker."""

from typing import Any

from arq.connections import ArqRedis


class ArqQueue:
    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue(self, job: str, *args: Any, job_id: str | None = None) -> None:
        # com job_id, enfileirar de novo a mesma tarefa enquanto ela roda não duplica o trabalho
        await self._pool.enqueue_job(job, *args, _job_id=job_id)


ACTIVE_TTL = 900


def active_key(user_id: object) -> str:
    """Marca no Redis que há leitura ou cotação em andamento para o usuário."""
    return f"sync:active:{user_id}"


def sync_user_job_id(user_id: object) -> str:
    return f"sync-user:{user_id}"


def sync_wallet_job_id(wallet_id: object) -> str:
    return f"sync-wallet:{wallet_id}"


def attest_job_id(report_id: object) -> str:
    return f"attest:{report_id}"
