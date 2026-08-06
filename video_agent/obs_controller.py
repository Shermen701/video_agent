from __future__ import annotations

import base64
from io import BytesIO
import logging
import subprocess
import socket
import time
from pathlib import Path

from video_agent.app_discovery import find_obs_executable
from video_agent.config import ObsConfig
from video_agent.models import CaptureTarget, ErrorCode
from video_agent.process_control import (
    shutdown_matching_processes,
    wait_for_matching_processes_exit,
)


CAPTURE_SCENE_NAME = "VideoAgent-DingTalk"
CAPTURE_INPUT_NAME = "VideoAgent-DingTalk-Window"
CAPTURE_AUDIO_INPUT_NAME = "VideoAgent-Application-Audio"
APPLICATION_AUDIO_CAPTURE_KIND = "wasapi_process_output_capture"

LOGGER = logging.getLogger("video_agent")


class ObsController:
    def __init__(self, config: ObsConfig) -> None:
        self.config = config
        self._client = None
        self._previous_scene: str | None = None

    def ensure_running(self) -> None:
        if self._is_websocket_port_open():
            return
        executable = find_obs_executable(self.config.executable_path)
        if executable is None:
            raise RuntimeError(f"{ErrorCode.OBS_START_FAILED.value}: OBS executable not found")
        # A WebSocket shutdown returns before all OBS helper processes have
        # released the single-instance lock.  Starting another instance in
        # that gap produces neither a usable server nor a clear error.
        if not wait_for_matching_processes_exit(
            executable_names={executable.name},
            exact_paths={executable.resolve()},
            timeout_seconds=5,
        ):
            shutdown_matching_processes(
                executable_names={executable.name},
                exact_paths={executable.resolve()},
                timeout_seconds=15,
            )
            if not wait_for_matching_processes_exit(
                executable_names={executable.name},
                exact_paths={executable.resolve()},
                timeout_seconds=5,
            ):
                raise RuntimeError(
                    f"{ErrorCode.OBS_START_FAILED.value}: previous OBS instance did not exit"
                )
        subprocess.Popen([str(executable), "--disable-shutdown-check"], cwd=str(executable.parent))
        time.sleep(min(self.config.startup_timeout_seconds, 5))

    def connect(self) -> None:
        # obsws-python logs connection parameters (including the password) and
        # a full traceback for every refused cold-start connection.  Keep SDK
        # internals quiet and report concise, redacted lifecycle messages here.
        logging.getLogger("obsws_python").setLevel(logging.CRITICAL + 1)
        try:
            from obsws_python import ReqClient  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("obsws-python is not installed; run pip install -r requirements.txt") from exc
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        last_error: Exception | None = None
        waiting_logged = False
        while time.monotonic() < deadline:
            if not self._is_websocket_port_open():
                if not waiting_logged:
                    LOGGER.info(
                        "waiting for OBS WebSocket: host=%s port=%s timeout=%ss",
                        self.config.websocket_host,
                        self.config.websocket_port,
                        self.config.startup_timeout_seconds,
                    )
                    waiting_logged = True
                self._dismiss_recovery_dialog_if_present()
                time.sleep(1)
                continue
            try:
                self._client = ReqClient(
                    host=self.config.websocket_host,
                    port=self.config.websocket_port,
                    password=self.config.websocket_password,
                    timeout=5,
                )
                LOGGER.info(
                    "connected to OBS WebSocket: host=%s port=%s",
                    self.config.websocket_host,
                    self.config.websocket_port,
                )
                return
            except Exception as exc:
                last_error = exc
                self._dismiss_recovery_dialog_if_present()
                time.sleep(1)
        if last_error is None:
            raise RuntimeError(
                f"{ErrorCode.OBS_WEBSOCKET_FAILED.value}: OBS WebSocket "
                f"{self.config.websocket_host}:{self.config.websocket_port} did not become ready "
                f"within {self.config.startup_timeout_seconds} seconds"
            )
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

    def configure_window_capture(self, target: CaptureTarget) -> None:
        """Switch OBS to a reusable scene that captures one native window."""
        self._require_client()
        if not target.title or not target.class_name or not target.executable_name:
            raise RuntimeError("recording_failed: incomplete meeting capture target")

        current = self._client.get_current_program_scene()  # type: ignore[union-attr]
        current_name = getattr(current, "current_program_scene_name", "")
        if not current_name:
            raise RuntimeError("recording_failed: OBS current scene is unavailable")
        self._previous_scene = str(current_name)

        scenes = self._client.get_scene_list().scenes  # type: ignore[union-attr]
        scene_names = {
            str(item.get("sceneName", "")) if isinstance(item, dict) else str(getattr(item, "scene_name", ""))
            for item in scenes
        }
        if CAPTURE_SCENE_NAME not in scene_names:
            self._client.create_scene(CAPTURE_SCENE_NAME)  # type: ignore[union-attr]

        settings = {
            "window": f"{target.title}:{target.class_name}:{target.executable_name}",
            "cursor": False,
            "client_area": True,
            "method": 2,
            "priority": 1,
        }
        inputs = self._client.get_input_list().inputs  # type: ignore[union-attr]
        input_names = {
            str(item.get("inputName", "")) if isinstance(item, dict) else str(getattr(item, "input_name", ""))
            for item in inputs
        }
        if CAPTURE_INPUT_NAME in input_names:
            self._client.set_input_settings(CAPTURE_INPUT_NAME, settings, True)  # type: ignore[union-attr]
            try:
                item = self._client.get_scene_item_id(CAPTURE_SCENE_NAME, CAPTURE_INPUT_NAME)  # type: ignore[union-attr]
                item_id = int(item.scene_item_id)
            except Exception:
                item = self._client.create_scene_item(CAPTURE_SCENE_NAME, CAPTURE_INPUT_NAME, True)  # type: ignore[union-attr]
                item_id = int(item.scene_item_id)
        else:
            item = self._client.create_input(  # type: ignore[union-attr]
                CAPTURE_SCENE_NAME,
                CAPTURE_INPUT_NAME,
                "window_capture",
                settings,
                True,
            )
            item_id = int(item.scene_item_id)

        video = self._client.get_video_settings()  # type: ignore[union-attr]
        width = float(getattr(video, "base_width"))
        height = float(getattr(video, "base_height"))
        self._client.set_scene_item_transform(  # type: ignore[union-attr]
            CAPTURE_SCENE_NAME,
            item_id,
            {
                "alignment": 5,
                "boundsAlignment": 0,
                "boundsType": "OBS_BOUNDS_SCALE_INNER",
                "boundsWidth": width,
                "boundsHeight": height,
                "cropLeft": 0,
                "cropTop": 0,
                "cropRight": 0,
                "cropBottom": 0,
                "positionX": 0.0,
                "positionY": 0.0,
            },
        )
        self._client.set_current_program_scene(CAPTURE_SCENE_NAME)  # type: ignore[union-attr]

    def restore_capture_scene(self) -> None:
        """Restore the scene that was active before window capture was configured."""
        if self._client is None or self._previous_scene is None:
            return
        previous_scene = self._previous_scene
        self._client.set_current_program_scene(previous_scene)
        self._previous_scene = None

    def configure_application_audio_capture(self, target: CaptureTarget) -> None:
        """Capture audio from one application instead of all desktop output."""
        self._require_client()
        if not target.title or not target.class_name or not target.executable_name:
            raise RuntimeError("recording_failed: incomplete application audio capture target")
        settings = {
            "window": f"{target.title}:{target.class_name}:{target.executable_name}",
            "priority": 1,
        }
        inputs = self._client.get_input_list().inputs  # type: ignore[union-attr]
        input_names = {
            str(item.get("inputName", "")) if isinstance(item, dict) else str(getattr(item, "input_name", ""))
            for item in inputs
        }
        if CAPTURE_AUDIO_INPUT_NAME in input_names:
            self._client.set_input_settings(CAPTURE_AUDIO_INPUT_NAME, settings, True)  # type: ignore[union-attr]
            try:
                self._client.get_scene_item_id(CAPTURE_SCENE_NAME, CAPTURE_AUDIO_INPUT_NAME)  # type: ignore[union-attr]
            except Exception:
                self._client.create_scene_item(CAPTURE_SCENE_NAME, CAPTURE_AUDIO_INPUT_NAME, True)  # type: ignore[union-attr]
            return
        self._client.create_input(  # type: ignore[union-attr]
            CAPTURE_SCENE_NAME,
            CAPTURE_AUDIO_INPUT_NAME,
            APPLICATION_AUDIO_CAPTURE_KIND,
            settings,
            True,
        )

    def verify_window_capture_visible(
        self,
        diagnostic_path: Path,
        duration_seconds: float = 5.0,
        sample_count: int = 3,
    ) -> bool:
        """Return false only when every sampled OBS source frame is pure black."""
        self._require_client()
        try:
            from PIL import Image  # type: ignore
        except ModuleNotFoundError as exc:
            raise RuntimeError("Pillow is required for OBS capture health checks") from exc

        samples = max(1, int(sample_count))
        interval = max(0.0, float(duration_seconds)) / samples
        black_images = []
        for _ in range(samples):
            if interval:
                time.sleep(interval)
            response = self._client.get_source_screenshot(  # type: ignore[union-attr]
                CAPTURE_INPUT_NAME,
                "png",
                320,
                180,
                -1,
            )
            image_data = str(getattr(response, "image_data", "") or "")
            encoded = image_data.split(",", 1)[-1]
            try:
                image = Image.open(BytesIO(base64.b64decode(encoded))).convert("L")
            except Exception as exc:
                raise RuntimeError("recording_failed: invalid OBS capture screenshot") from exc
            if image.getextrema()[1] > 4:
                return True
            black_images.append(image.copy())

        diagnostic_path.parent.mkdir(parents=True, exist_ok=True)
        black_images[-1].save(diagnostic_path, format="PNG")
        return False

    def shutdown_application(self) -> None:
        """Stop OBS safely and close the path-verified OBS process."""
        if self._client is not None:
            try:
                self.stop_recording()
            except Exception:
                pass
            try:
                self.restore_capture_scene()
            except Exception:
                pass
            # OBS WebSocket's Shutdown request lets OBS flush its state and
            # close cleanly.  Posting WM_CLOSE alone may race that work and
            # trigger the next-launch recovery dialog.
            try:
                self._client.shutdown()  # type: ignore[union-attr]
            except Exception:
                pass
        executable = find_obs_executable(self.config.executable_path)
        if executable is not None:
            shutdown_matching_processes(
                executable_names={executable.name},
                exact_paths={executable.resolve()},
                timeout_seconds=15,
            )
        self._client = None

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

    def _dismiss_recovery_dialog_if_present(self) -> bool:
        """Choose normal start only on OBS's abnormal-shutdown dialog."""
        try:
            from pywinauto import Desktop  # type: ignore

            return self._click_normal_start_on_recovery_dialog(Desktop(backend="uia"))
        except Exception:
            return False

    @staticmethod
    def _click_normal_start_on_recovery_dialog(desktop: object) -> bool:
        try:
            windows = desktop.windows(visible_only=True)  # type: ignore[attr-defined]
        except Exception:
            return False
        for window in windows:
            try:
                controls = list(window.descendants())
                texts = [str(window.window_text() or "")]
                texts.extend(str(control.window_text() or "") for control in controls)
            except Exception:
                continue
            # Require both choices so an unrelated "normal start" action is
            # never clicked.  OBS 32 uses “以…模式运行”; older builds use
            # the shorter “启动” wording.
            has_safe = any(
                marker in text
                for text in texts
                for marker in ("安全启动", "以安全模式运行")
            )
            has_normal = any(
                marker in text
                for text in texts
                for marker in ("正常启动", "以正常模式运行")
            )
            if not (has_safe and has_normal):
                continue
            for control in controls:
                try:
                    if str(control.window_text() or "").strip() in {
                        "正常启动",
                        "以正常模式运行",
                    }:
                        control.click_input()
                        return True
                except Exception:
                    continue
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
