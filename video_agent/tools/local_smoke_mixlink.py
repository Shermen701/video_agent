"""Run a real MixLink recording smoke test without polling the platform."""
from __future__ import annotations

import argparse
import os
from dataclasses import replace
from datetime import timedelta

from video_agent.agent import RecordingAgent
from video_agent.config import load_config
from video_agent.models import Credentials, MeetingInfo, RecordingTask, TaskStatus, utc_now
from video_agent.obs_controller import ObsController
from video_agent.runtime_paths import apply_runtime_paths
from video_agent.uploader import MinioUploader


class LocalSmokePlatform:
    """Print status transitions instead of writing callbacks to the platform."""

    def report_status(
        self,
        task_id: str,
        status: TaskStatus,
        message: str = "",
        extra=None,
        failure=None,
    ) -> None:
        print(f"[{status.value}] {message}", flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--record-seconds", type=int, default=15)
    parser.add_argument("--meeting-password", default=os.environ.get("VIDEO_AGENT_SMOKE_MEETING_PASSWORD", ""))
    args = parser.parse_args(argv)

    account = _required_env("VIDEO_AGENT_SMOKE_ACCOUNT")
    password = _required_env("VIDEO_AGENT_SMOKE_PASSWORD")
    meeting_no = _required_env("VIDEO_AGENT_SMOKE_MEETING_NO")
    if args.record_seconds <= 0:
        raise SystemExit("--record-seconds must be positive")

    config = apply_runtime_paths(load_config(args.config))
    config = replace(config, agent=replace(config.agent, close_apps_after_task=True))
    now = utc_now()
    task = RecordingTask(
        id=f"local-smoke-mixlink-{now.strftime('%Y%m%d%H%M%S')}",
        title="MixLink local smoke",
        start_time=now,
        end_time=now + timedelta(minutes=5),
        credentials=Credentials(account=account, password=password),
        meeting=MeetingInfo(meeting_no=meeting_no, password=args.meeting_password),
        meeting_provider="mixlink",
        upload_target="minio",
    )
    agent = RecordingAgent(
        config,
        platform=LocalSmokePlatform(),  # type: ignore[arg-type]
        obs=ObsController(config.obs),
        uploader=MinioUploader(config.minio),
    )
    agent.execute_task(task, smoke_record_seconds=args.record_seconds)
    print(f"task_id={task.id}", flush=True)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"set {name} before running the smoke test")
    return value


if __name__ == "__main__":
    main()
