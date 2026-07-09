from __future__ import annotations

import subprocess
import socket
import time
from pathlib import Path

from video_agent.app_discovery import find_obs_executable
from video_agent.config import ObsConfig
from video_agent.models import ErrorCode


class ObsController:
    def __init__(self, config: ObsConfig) -> None:
        self.config = config
        self._client = None

    def ensure_running(self) -> None:
        if self._is_websocket_port_open():
            return
        executable = find_obs_executable(self.config.executable_path)
        if executable is None:
            raise RuntimeError(f"{ErrorCode.OBS_START_FAILED.value}: OBS executable not found")
        subprocess.Popen([str(executable), "--disable-shutdown-check"], cwd=str(executable.parent))
        time.sleep(min(self.config.startup_timeout_seconds, 5))

    def connect(self) -> None:
        try:
            from obsws_python import ReqClient  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("obsws-python is not installed; run pip install -r requirements.txt") from exc
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._client = ReqClient(
                    host=self.config.websocket_host,
                    port=self.config.websocket_port,
                    password=self.config.websocket_password,
                    timeout=5,
                )
                return
            except Exception as exc:
                last_error = exc
                time.sleep(1)
        raise RuntimeError(f"{ErrorCode.OBS_WEBSOCKET_FAILED.value}: {last_error}") from last_error

    def start_recording(self, task_dir: Path) -> None:
        self._require_client()
        task_dir = task_dir.resolve()
        task_dir.mkdir(parents=True, exist_ok=True)
        self._client.set_record_directory(str(task_dir))  # type: ignore[union-attr]
        status = self._client.get_record_status()  # type: ignore[union-attr]
        if not getattr(status, "output_active", False):
            self._client.start_record()  # type: ignore[union-attr]
            self._wait_for_recording_state(active=True)

    def stop_recording(self) -> None:
        self._require_client()
        status = self._client.get_record_status()  # type: ignore[union-attr]
        if getattr(status, "output_active", False):
            self._client.stop_record()  # type: ignore[union-attr]
            self._wait_for_recording_state(active=False)

    def find_latest_recording(self, task_dir: Path) -> Path | None:
        files = [path for path in task_dir.glob("*") if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".mov", ".flv"}]
        if not files:
            return None
        return max(files, key=lambda path: path.stat().st_mtime)

    def _require_client(self) -> None:
        if self._client is None:
            raise RuntimeError(f"{ErrorCode.OBS_WEBSOCKET_FAILED.value}: OBS websocket is not connected")

    def _is_websocket_port_open(self) -> bool:
        try:
            with socket.create_connection((self.config.websocket_host, self.config.websocket_port), timeout=1):
                return True
        except OSError:
            return False

    def _wait_for_recording_state(self, active: bool) -> None:
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() < deadline:
            status = self._client.get_record_status()  # type: ignore[union-attr]
            if getattr(status, "output_active", False) is active:
                return
            time.sleep(0.5)
        state = "start" if active else "stop"
        raise RuntimeError(f"{ErrorCode.OBS_WEBSOCKET_FAILED.value}: OBS recording did not {state}")
