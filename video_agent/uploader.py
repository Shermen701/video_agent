from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Protocol

from video_agent.config import MinioConfig
from video_agent.models import UploadResult
from video_agent.platform_client import PlatformClient


class MinioLike(Protocol):
    def fput_object(self, bucket_name: str, object_name: str, file_path: str) -> object:
        ...


class ChunkedUploader:
    def __init__(self, platform: PlatformClient, part_size_bytes: int, retry_count: int) -> None:
        self.platform = platform
        self.part_size_bytes = part_size_bytes
        self.retry_count = retry_count

    def upload(self, task_id: str, file_path: Path) -> str:
        upload_id = self.platform.init_upload(task_id, file_path, self.part_size_bytes)
        part_no = 1
        with file_path.open("rb") as handle:
            while True:
                chunk = handle.read(self.part_size_bytes)
                if not chunk:
                    break
                self._upload_part_with_retry(upload_id, part_no, chunk)
                part_no += 1
        self.platform.complete_upload(upload_id, task_id)
        return upload_id

    def _upload_part_with_retry(self, upload_id: str, part_no: int, chunk: bytes) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.retry_count + 1):
            try:
                self.platform.upload_part(upload_id, part_no, chunk)
                return
            except Exception as exc:
                last_error = exc
                if attempt < self.retry_count:
                    time.sleep(min(2**attempt, 10))
        raise RuntimeError(f"failed to upload part {part_no}") from last_error


class MinioUploader:
    def __init__(self, config: MinioConfig, client: MinioLike | None = None) -> None:
        self.config = config
        self.client = client or self._create_client(config)

    def upload(self, task_id: str, file_path: Path) -> UploadResult:
        object_name = self._object_name(task_id, file_path)
        self.client.fput_object(self.config.bucket_name, object_name, str(file_path))
        return UploadResult(
            storage_path=f"/{object_name}",
            file_size=file_path.stat().st_size,
            file_checksum=_sha256(file_path),
            filename=file_path.name,
        )

    def _object_name(self, task_id: str, file_path: Path) -> str:
        prefix = self.config.object_prefix.strip("/")
        if prefix:
            return f"{prefix}/{task_id}/{file_path.name}"
        return f"{task_id}/{file_path.name}"

    @staticmethod
    def _create_client(config: MinioConfig) -> MinioLike:
        if not config.endpoint or not config.access_key or not config.secret_key:
            raise RuntimeError("minio endpoint/access_key/secret_key must be configured")
        try:
            from minio import Minio
        except ModuleNotFoundError as exc:
            raise RuntimeError("minio package is required for MinIO uploads") from exc
        endpoint = config.endpoint.removeprefix("http://").removeprefix("https://").rstrip("/")
        return Minio(endpoint, access_key=config.access_key, secret_key=config.secret_key, secure=config.secure)


def _sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()
