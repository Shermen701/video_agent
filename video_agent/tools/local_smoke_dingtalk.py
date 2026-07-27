"""Run a real DingTalk recording smoke test without polling the platform."""
from __future__ import annotations

import argparse
import getpass
import os
import time
from dataclasses import replace
from datetime import timedelta

from video_agent.agent import RecordingAgent
from video_agent.config import load_config
from video_agent.models import Credentials, MeetingInfo, RecordingTask, TaskStatus, utc_now
from video_agent.obs_controller import ObsController
from video_agent.providers.dingtalk import DingTalkProvider
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
    parser.add_argument(
        "--prompt-meeting-password",
        action="store_true",
        help="Prompt for the meeting password without echoing it to the terminal.",
    )
    parser.add_argument("--meeting-no", default=os.environ.get("VIDEO_AGENT_SMOKE_MEETING_NO", ""))
    parser.add_argument(
        "--prompt-credentials",
        action="store_true",
        help="Prompt for DingTalk credentials for a full local recording smoke test.",
    )
    parser.add_argument(
        "--login-only",
        action="store_true",
        help="Prompt for DingTalk credentials and verify only the login flow.",
    )
    parser.add_argument(
        "--login-and-open-meeting-home",
        action="store_true",
        help="Prompt for credentials, log in, then verify the left-side Meeting navigation.",
    )
    parser.add_argument(
        "--login-and-open-join-dialog",
        action="store_true",
        help="Prompt for credentials, then verify Meeting navigation and the Join Meeting card.",
    )
    args = parser.parse_args(argv)
    smoke_modes = (
        args.login_only,
        args.login_and_open_meeting_home,
        args.login_and_open_join_dialog,
    )
    if sum(smoke_modes) > 1:
        raise SystemExit("use only one login smoke option")
    if not any(smoke_modes) and args.record_seconds <= 0:
        raise SystemExit("--record-seconds must be positive")

    config = apply_runtime_paths(load_config(args.config))
    if any(smoke_modes):
        credentials = _prompt_login_credentials()
        provider = DingTalkProvider(dict(config.providers.get("dingtalk") or {}))
        provider.launch()
        provider.ensure_logged_in(credentials)
        if args.login_and_open_meeting_home or args.login_and_open_join_dialog:
            provider._connect_window(timeout_seconds=5)
            provider._navigate_to_meeting_home()
            _wait_for_meeting_home(provider)
        if args.login_and_open_join_dialog:
            provider._click_join_meeting_card()
            if provider._find_join_dialog_window(timeout_seconds=8) is None:
                raise RuntimeError("DingTalk join dialog did not appear after meeting-card click")
            print("DingTalk join-card smoke succeeded", flush=True)
            return
        if args.login_and_open_meeting_home:
            print("DingTalk meeting navigation smoke succeeded", flush=True)
            return
        print("DingTalk login smoke succeeded", flush=True)
        return

    credentials = _prompt_login_credentials() if args.prompt_credentials else Credentials(
        _required_env("VIDEO_AGENT_SMOKE_ACCOUNT"), _required_env("VIDEO_AGENT_SMOKE_PASSWORD")
    )
    meeting_no = str(args.meeting_no or "").strip()
    if not meeting_no:
        raise SystemExit("set --meeting-no or VIDEO_AGENT_SMOKE_MEETING_NO before running the smoke test")
    meeting_password = (
        getpass.getpass("Meeting password: ")
        if args.prompt_meeting_password
        else args.meeting_password
    )
    now = utc_now()
    task = RecordingTask(
        id=f"local-smoke-dingtalk-{now.strftime('%Y%m%d%H%M%S')}",
        title="DingTalk local smoke",
        start_time=now,
        end_time=now + timedelta(minutes=5),
        credentials=credentials,
        meeting=MeetingInfo(meeting_no, meeting_password),
        meeting_provider="dingtalk",
        upload_target="minio",
    )
    config = replace(config, agent=replace(config.agent, close_apps_after_task=True))
    RecordingAgent(config, platform=LocalSmokePlatform(), obs=ObsController(config.obs), uploader=MinioUploader(config.minio)).execute_task(task, smoke_record_seconds=args.record_seconds)
    print(f"task_id={task.id}", flush=True)


def _prompt_login_credentials() -> Credentials:
    """Prompt without putting credentials in command history or source files."""
    account = input("DingTalk account: ").strip()
    password = getpass.getpass("DingTalk password: ")
    if not account or not password:
        raise SystemExit("DingTalk account and password are required")
    return Credentials(account=account, password=password)


def _wait_for_meeting_home(provider: DingTalkProvider, timeout_seconds: float = 15) -> None:
    deadline = time.monotonic() + timeout_seconds
    markers = ["加入会议", "发起会议", "预约会议"]
    while time.monotonic() < deadline:
        if provider._has_text(markers):
            return
        time.sleep(0.5)
    raise RuntimeError("DingTalk meeting home did not appear after navigation")


if __name__ == "__main__":
    main()
