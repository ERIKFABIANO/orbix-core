"""Arquivos de relatório no Cloudflare R2 (API compatível com S3). Bucket privado."""

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from starlette.concurrency import run_in_threadpool

from orbix.config import Settings
from orbix.errors import AppError

log = structlog.get_logger()

PRESIGN_TTL = 600


class Storage(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def presign(self, key: str, filename: str) -> tuple[str, datetime]: ...
    async def delete_prefix(self, prefix: str) -> None: ...


class R2Storage:
    def __init__(self, settings: Settings) -> None:
        if not settings.storage_enabled:
            raise RuntimeError("R2 não configurado")
        if settings.r2_access_key_id is None or settings.r2_secret_access_key is None:
            raise RuntimeError("R2 não configurado")
        self._bucket = settings.r2_bucket
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_access_key_id.get_secret_value(),
            aws_secret_access_key=settings.r2_secret_access_key.get_secret_value(),
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=15,
            ),
        )

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        try:
            await run_in_threadpool(
                self._client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except (BotoCoreError, ClientError):
            log.exception("falha ao gravar no R2")
            raise AppError("storage_unavailable", 503) from None

    async def presign(self, key: str, filename: str) -> tuple[str, datetime]:
        # link curto e que força download com o nome certo; o bucket continua privado
        try:
            url = await run_in_threadpool(
                self._client.generate_presigned_url,
                "get_object",
                Params={
                    "Bucket": self._bucket,
                    "Key": key,
                    "ResponseContentDisposition": f'attachment; filename="{filename}"',
                },
                ExpiresIn=PRESIGN_TTL,
            )
        except (BotoCoreError, ClientError):
            raise AppError("storage_unavailable", 503) from None
        return str(url), datetime.now(UTC) + timedelta(seconds=PRESIGN_TTL)

    async def delete_prefix(self, prefix: str) -> None:
        def _delete() -> None:
            listing = self._client.list_objects_v2(Bucket=self._bucket, Prefix=prefix)
            keys = [{"Key": item["Key"]} for item in listing.get("Contents", [])]
            if keys:
                self._client.delete_objects(Bucket=self._bucket, Delete={"Objects": keys})

        try:
            await run_in_threadpool(_delete)
        except (BotoCoreError, ClientError):
            log.exception("falha ao apagar arquivos do R2", prefix=prefix)


def user_prefix(user_id: object) -> str:
    return f"reports/{user_id}/"
