"""Run a real DingTalk recording smoke test without polling the platform."""
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
from video_agent.tools.local_smoke_mixlink import _required_env
from video_agent.uploader import MinioUploader


class LocalSmokePlatform:
    def report_status(self, task_id: str, status: TaskStatus, message: str = "", extra=None, failure=None) -> None:
        print(f"[{status.value}] {message}", flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--record-seconds", type=int, default=15)
    parser.add_argument("--meeting-password", default=os.environ.get("VIDEO_AGENT_SMOKE_MEETING_PASSWORD", ""))
    args = parser.parse_args(argv)
    if args.record_seconds <= 0:
        raise SystemExit("--record-seconds must be positive")

    now = utc_now()
    task = RecordingTask(
        id=f"local-smoke-dingtalk-{now.strftime('%Y%m%d%H%M%S')}",
        title="DingTalk local smoke",
        start_time=now,
        end_time=now + timedelta(minutes=5),
        credentials=Credentials(_required_env("VIDEO_AGENT_SMOKE_ACCOUNT"), _required_env("VIDEO_AGENT_SMOKE_PASSWORD")),
        meeting=MeetingInfo(_required_env("VIDEO_AGENT_SMOKE_MEETING_NO"), args.meeting_password),
        meeting_provider="dingtalk",
        upload_target="minio",
    )
    config = apply_runtime_paths(load_config(args.config))
    config = replace(config, agent=replace(config.agent, close_apps_after_task=True))
    RecordingAgent(config, platform=LocalSmokePlatform(), obs=ObsController(config.obs), uploader=MinioUploader(config.minio)).execute_task(task, smoke_record_seconds=args.record_seconds)
    print(f"task_id={task.id}", flush=True)


if __name__ == "__main__":
    main()
