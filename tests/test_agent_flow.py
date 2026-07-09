from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from video_agent.agent import RecordingAgent
from video_agent.config import AgentConfig, AppConfig, ObsConfig, UploadConfig
from video_agent.models import Credentials, MeetingInfo, RecordingTask, TaskStatus, UploadResult, utc_now


class FakePlatform:
    def __init__(self, task: RecordingTask) -> None:
        self.task = task
        self.statuses: list[TaskStatus] = []

    def get_next_task(self, agent_id: str) -> RecordingTask:
        return self.task

    def report_status(self, task_id: str, status: TaskStatus, message: str = "", extra=None, failure=None) -> None:
        self.statuses.append(status)

    def init_upload(self, task_id: str, file_path: Path, part_size: int) -> str:
        return "upload-1"

    def upload_part(self, upload_id: str, part_no: int, data: bytes) -> None:
        return None

    def complete_upload(self, upload_id: str, task_id: str) -> None:
        return None


class FakeObs:
    def ensure_running(self) -> None:
        return None

    def connect(self) -> None:
        return None

    def start_recording(self, task_dir: Path) -> None:
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "recording.mp4").write_bytes(b"video")

    def stop_recording(self) -> None:
        return None

    def find_latest_recording(self, task_dir: Path) -> Path | None:
        return task_dir / "recording.mp4"


class FakeProvider:
    def __init__(self) -> None:
        self.wait_deadline = None

    def launch(self) -> None:
        return None

    def ensure_logged_in(self, credentials: Credentials) -> None:
        return None

    def join(self, meeting: MeetingInfo) -> None:
        return None

    def prepare_audio_video(self) -> None:
        return None

    def wait_until_finished(self, deadline) -> None:
        self.wait_deadline = deadline
        return None

    def capture_diagnostics(self, task_dir: Path) -> Path | None:
        return None

    def cleanup(self) -> None:
        return None


class FakeUploader:
    def upload(self, task_id: str, file_path: Path) -> UploadResult:
        return UploadResult(
            storage_path=f"/external-record/{task_id}/{file_path.name}",
            file_size=file_path.stat().st_size,
            file_checksum="abc123",
            filename=file_path.name,
        )


class AgentFlowTest(unittest.TestCase):
    def test_executes_successful_task(self) -> None:
        now = utc_now()
        task = RecordingTask(
            id="task-1",
            start_time=now + timedelta(minutes=1),
            end_time=now + timedelta(minutes=10),
            credentials=Credentials("account", "password"),
            meeting=MeetingInfo("123", "0000"),
        )
        workspace_tmp = Path("test_outputs") / "agent_flow"
        workspace_tmp.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            agent=AgentConfig(prepare_before_minutes=5),
            obs=ObsConfig(recordings_dir=str(workspace_tmp)),
            upload=UploadConfig(part_size_bytes=2, retry_count=1),
        )
        platform = FakePlatform(task)
        agent = RecordingAgent(config, platform=platform, obs=FakeObs(), uploader=FakeUploader())  # type: ignore[arg-type]

        with patch("video_agent.agent.create_provider", return_value=FakeProvider()):
            agent.run_once()

        self.assertEqual(
            platform.statuses,
            [TaskStatus.PREPARING, TaskStatus.JOINING, TaskStatus.RECORDING, TaskStatus.UPLOADING, TaskStatus.COMPLETED],
        )
        metadata = workspace_tmp / "task-1" / "task_metadata.json"
        self.assertIn('"password": "***"', metadata.read_text(encoding="utf-8"))

    def test_smoke_record_seconds_overrides_recording_deadline(self) -> None:
        now = utc_now()
        task = RecordingTask(
            id="task-smoke",
            start_time=now - timedelta(minutes=1),
            end_time=now + timedelta(minutes=30),
            credentials=Credentials("account", "password"),
            meeting=MeetingInfo("123", "0000"),
        )
        workspace_tmp = Path("test_outputs") / "agent_flow"
        workspace_tmp.mkdir(parents=True, exist_ok=True)
        config = AppConfig(
            agent=AgentConfig(prepare_before_minutes=5),
            obs=ObsConfig(recordings_dir=str(workspace_tmp)),
            upload=UploadConfig(part_size_bytes=2, retry_count=1),
        )
        platform = FakePlatform(task)
        agent = RecordingAgent(config, platform=platform, obs=FakeObs(), uploader=FakeUploader())  # type: ignore[arg-type]
        provider = FakeProvider()

        with patch("video_agent.agent.create_provider", return_value=provider):
            before = utc_now()
            agent.run_once(smoke_record_seconds=300)
            after = utc_now()

        self.assertIsNotNone(provider.wait_deadline)
        self.assertGreaterEqual(provider.wait_deadline, before + timedelta(seconds=295))
        self.assertLessEqual(provider.wait_deadline, after + timedelta(seconds=305))
        self.assertLess(provider.wait_deadline, task.end_time)


if __name__ == "__main__":
    unittest.main()
