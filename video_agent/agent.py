from __future__ import annotations

import json
import logging
import time
from datetime import timedelta
from pathlib import Path

from video_agent.config import AppConfig, load_config, parse_config_arg
from video_agent.models import ErrorCode, FailureReport, RecordingTask, TaskPaths, TaskStatus, utc_now
from video_agent.obs_controller import ObsController
from video_agent.platform_client import PlatformClient
from video_agent.providers.registry import create_provider
from video_agent.redaction import redact_mapping
from video_agent.runtime_paths import apply_runtime_paths, ensure_runtime_files
from video_agent.task_scheduler import is_expired, should_prepare
from video_agent.uploader import MinioUploader

LOGGER = logging.getLogger("video_agent")


class RecordingAgent:
    def __init__(
        self,
        config: AppConfig,
        platform: PlatformClient | None = None,
        obs: ObsController | None = None,
        uploader: MinioUploader | None = None,
    ) -> None:
        self.config = config
        self.platform = platform or PlatformClient(config.platform)
        self.obs = obs or ObsController(config.obs)
        self.uploader = uploader or MinioUploader(config.minio)

    def run_forever(self) -> None:
        while True:
            self.run_once()
            time.sleep(self.config.agent.poll_interval_seconds)

    def run_once(self, smoke_record_seconds: int | None = None) -> None:
        task = self.platform.get_next_task(self.config.agent.agent_id)
        if task is None:
            LOGGER.info("no task available")
            return
        self._log_task_preflight(task, smoke_record_seconds)
        now = utc_now()
        if is_expired(task, now) and smoke_record_seconds is None:
            self._report_failure(task, self._paths(task), ErrorCode.TASK_EXPIRED, "task end_time has passed")
            return
        prepare_at = task.start_time - timedelta(minutes=self.config.agent.prepare_before_minutes)
        if smoke_record_seconds is None and not should_prepare(task, now, self.config.agent.prepare_before_minutes):
            LOGGER.info(
                "task %s is not ready yet: now=%s start=%s prepare_at=%s",
                task.id,
                now.isoformat(),
                task.start_time.isoformat(),
                prepare_at.isoformat(),
            )
            return
        self.execute_task(task, smoke_record_seconds=smoke_record_seconds)

    def execute_task(self, task: RecordingTask, smoke_record_seconds: int | None = None) -> None:
        paths = self._paths(task)
        provider = None
        record_start_time = None
        record_end_time = None
        recording_started = False
        capture_configured = False
        obs_touched = False
        paths.task_dir.mkdir(parents=True, exist_ok=True)
        paths.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self._write_metadata(task, paths)
        try:
            provider = create_provider(task.meeting_provider, self.config.providers)
            obs_touched = True
            self.platform.report_status(task.id, TaskStatus.PREPARING, "preparing local applications")
            self.obs.ensure_running()
            self.obs.connect()
            provider.launch()

            self.platform.report_status(task.id, TaskStatus.JOINING, "joining meeting")
            provider.ensure_logged_in(task.credentials)
            provider.join(task.meeting)
            provider.prepare_audio_video()

            get_capture_target = getattr(provider, "get_capture_target", None)
            capture_target = get_capture_target() if callable(get_capture_target) else None
            if capture_target is not None:
                self.obs.configure_window_capture(capture_target)
                capture_configured = True

            self.obs.start_recording(paths.task_dir)
            recording_started = True
            record_start_time = utc_now()
            self.platform.report_status(
                task.id,
                TaskStatus.RECORDING,
                "recording started",
                {"task": task, "record_start_time": record_start_time},
            )
            recording_deadline = task.end_time
            if smoke_record_seconds is not None:
                recording_deadline = record_start_time + timedelta(seconds=smoke_record_seconds)
                LOGGER.info(
                    "smoke recording enabled for task %s: stopping around %s",
                    task.id,
                    recording_deadline.isoformat(),
                )
            provider.wait_until_finished(recording_deadline)
            self.obs.stop_recording()
            recording_started = False
            record_end_time = utc_now()
            if capture_configured:
                self.obs.restore_capture_scene()
                capture_configured = False

            recording = self.obs.find_latest_recording(paths.task_dir)
            if recording is None:
                raise RuntimeError(f"{ErrorCode.NO_RECORDING_FILE.value}: no OBS recording file found")

            self.platform.report_status(task.id, TaskStatus.UPLOADING, "uploading recording", {"file": str(recording)})
            upload = self.uploader.upload(task.id, recording)
            duration = int((record_end_time - record_start_time).total_seconds()) if record_start_time else None
            self.platform.report_status(
                task.id,
                TaskStatus.COMPLETED,
                "recording uploaded",
                {
                    "task": task,
                    "upload": upload,
                    "record_start_time": record_start_time,
                    "record_end_time": record_end_time,
                    "duration": duration,
                    "record_method": "OBS",
                },
            )
        except Exception as exc:
            LOGGER.exception(
                "task execution failed before failure callback: id=%s provider=%s",
                task.id,
                task.meeting_provider,
            )
            screenshot = None
            if provider is not None:
                try:
                    screenshot = provider.capture_diagnostics(paths.screenshots_dir)
                except Exception:
                    LOGGER.exception("failed to capture provider diagnostics")
            self._report_failure(task, paths, _classify_error(exc), str(exc), screenshot, record_start_time, record_end_time)
        finally:
            if recording_started:
                try:
                    self.obs.stop_recording()
                except Exception:
                    LOGGER.exception("failed to stop OBS recording during cleanup")
            if capture_configured:
                try:
                    self.obs.restore_capture_scene()
                except Exception:
                    LOGGER.exception("failed to restore OBS scene during cleanup")
            if provider is not None:
                if self.config.agent.close_apps_after_task:
                    try:
                        provider.shutdown_application()
                    except Exception:
                        LOGGER.exception("provider application shutdown failed")
                try:
                    provider.cleanup()
                except Exception:
                    LOGGER.exception("provider cleanup failed")
            if self.config.agent.close_apps_after_task and obs_touched:
                try:
                    self.obs.shutdown_application()
                except Exception:
                    LOGGER.exception("OBS application shutdown failed")

    def _report_failure(
        self,
        task: RecordingTask,
        paths: TaskPaths,
        error_code: ErrorCode | str,
        message: str,
        screenshot_path: Path | None = None,
        record_start_time=None,
        record_end_time=None,
    ) -> None:
        failure = FailureReport(
            error_code=error_code,
            message=message,
            log_path=str(paths.log_path),
            screenshot_path=str(screenshot_path) if screenshot_path else None,
        )
        try:
            self.platform.report_status(
                task.id,
                TaskStatus.FAILED,
                message,
                extra={
                    "task": task,
                    "record_start_time": record_start_time,
                    "record_end_time": record_end_time,
                    "record_method": "OBS",
                },
                failure=failure,
            )
        except Exception:
            LOGGER.exception("failed to report task failure")

    def _paths(self, task: RecordingTask) -> TaskPaths:
        base_dir = Path(self.config.obs.recordings_dir)
        task_dir = base_dir / task.id
        return TaskPaths(
            task_dir=task_dir,
            metadata_path=task_dir / "task_metadata.json",
            log_path=task_dir / "agent.log",
            screenshots_dir=task_dir / "diagnostics",
        )

    def _write_metadata(self, task: RecordingTask, paths: TaskPaths) -> None:
        payload = {
            "id": task.id,
            "meeting_provider": task.meeting_provider,
            "title": task.title,
            "start_time": task.start_time.isoformat(),
            "end_time": task.end_time.isoformat(),
            "upload_target": task.upload_target,
            "raw": task.raw,
            "credentials": {"account": task.credentials.account, "password": task.credentials.password},
            "meeting": {
                "meeting_no": task.meeting.meeting_no,
                "password": task.meeting.password,
                "extra": task.meeting.extra,
            },
        }
        paths.metadata_path.write_text(json.dumps(redact_mapping(payload), ensure_ascii=False, indent=2), encoding="utf-8")

    def _log_task_preflight(self, task: RecordingTask, smoke_record_seconds: int | None) -> None:
        raw_password = str(task.raw.get("loginPassword") or "")
        password_decrypted = bool(raw_password and raw_password != task.credentials.password)
        LOGGER.info("task fetched: id=%s title=%s provider=%s", task.id, task.title, task.meeting_provider)
        LOGGER.info(
            "task account check: account=%s expected_13117414114=%s",
            task.credentials.account,
            task.credentials.account == "13117414114",
        )
        LOGGER.info("task password check: decrypted=%s nonempty=%s", password_decrypted, bool(task.credentials.password))
        if smoke_record_seconds is not None:
            LOGGER.info("smoke mode: record_seconds=%s", smoke_record_seconds)


def _classify_error(exc: Exception) -> ErrorCode:
    text = str(exc)
    for code in ErrorCode:
        if code.value in text:
            return code
    if "OBS" in text or "obsws" in text:
        return ErrorCode.OBS_WEBSOCKET_FAILED
    if "upload" in text.lower():
        return ErrorCode.UPLOAD_FAILED
    if "join" in text.lower():
        return ErrorCode.MEETING_JOIN_FAILED
    if "login" in text.lower():
        return ErrorCode.MEETING_LOGIN_FAILED
    return ErrorCode.INTERNAL_ERROR


def main(argv: list[str] | None = None) -> None:
    args = parse_config_arg(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.config:
        ensure_runtime_files()
        config_path = args.config
    else:
        config_path = ensure_runtime_files()
    config = apply_runtime_paths(load_config(config_path))
    LOGGER.info("using config: %s", config_path)
    LOGGER.info("recordings dir: %s", config.obs.recordings_dir)
    if args.init_only:
        LOGGER.info("runtime files initialized")
        return
    agent = RecordingAgent(config)
    if args.once:
        agent.run_once(smoke_record_seconds=args.smoke_record_seconds)
    else:
        if args.smoke_record_seconds is not None:
            raise SystemExit("--smoke-record-seconds must be used with --once")
        agent.run_forever()


if __name__ == "__main__":
    main()
