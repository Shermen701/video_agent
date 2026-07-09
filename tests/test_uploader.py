from __future__ import annotations

import unittest
from pathlib import Path

from video_agent.config import MinioConfig
from video_agent.uploader import ChunkedUploader, MinioUploader


class FakePlatform:
    def __init__(self) -> None:
        self.parts: list[tuple[int, bytes]] = []
        self.completed = False

    def init_upload(self, task_id: str, file_path: Path, part_size: int) -> str:
        self.init_args = (task_id, file_path.name, part_size)
        return "upload-1"

    def upload_part(self, upload_id: str, part_no: int, data: bytes) -> None:
        self.parts.append((part_no, data))

    def complete_upload(self, upload_id: str, task_id: str) -> None:
        self.completed = True


class UploaderTest(unittest.TestCase):
    def test_uploads_file_in_chunks(self) -> None:
        workspace_tmp = Path("test_outputs") / "uploader"
        workspace_tmp.mkdir(parents=True, exist_ok=True)
        path = workspace_tmp / "recording.mp4"
        path.write_bytes(b"abcdef")
        platform = FakePlatform()
        uploader = ChunkedUploader(platform, part_size_bytes=2, retry_count=1)  # type: ignore[arg-type]

        upload_id = uploader.upload("task-1", path)

        self.assertEqual(upload_id, "upload-1")
        self.assertEqual(platform.parts, [(1, b"ab"), (2, b"cd"), (3, b"ef")])
        self.assertTrue(platform.completed)

    def test_minio_upload_returns_storage_metadata(self) -> None:
        workspace_tmp = Path("test_outputs") / "uploader"
        workspace_tmp.mkdir(parents=True, exist_ok=True)
        path = workspace_tmp / "minio-recording.mp4"
        path.write_bytes(b"video")
        client = FakeMinio()
        uploader = MinioUploader(
            MinioConfig(endpoint="http://10.121.9.6:9000/", access_key="ak", secret_key="sk"),
            client=client,
        )

        result = uploader.upload("task-1", path)

        self.assertEqual(client.uploads, [("xny-iectp", "external-record/task-1/minio-recording.mp4", str(path))])
        self.assertEqual(result.storage_path, "/external-record/task-1/minio-recording.mp4")
        self.assertEqual(result.file_size, 5)
        self.assertEqual(result.filename, "minio-recording.mp4")
        self.assertEqual(len(result.file_checksum), 64)


class FakeMinio:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str]] = []

    def fput_object(self, bucket_name: str, object_name: str, file_path: str) -> object:
        self.uploads.append((bucket_name, object_name, file_path))
        return object()


if __name__ == "__main__":
    unittest.main()
