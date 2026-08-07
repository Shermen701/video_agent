from __future__ import annotations

import base64
from io import BytesIO
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from video_agent.config import ObsConfig
from video_agent.models import CaptureTarget
from video_agent.obs_controller import (
    CAPTURE_AUDIO_INPUT_NAME,
    CAPTURE_INPUT_NAME,
    CAPTURE_SCENE_NAME,
    APPLICATION_AUDIO_CAPTURE_KIND,
    ObsController,
)


class FakeObsClient:
    def __init__(self, scene_exists: bool = False, input_exists: bool = False) -> None:
        self.scene_exists = scene_exists
        self.input_exists = input_exists
        self.calls: list[tuple] = []
        self.screenshots: list[str] = []

    def get_current_program_scene(self):
        return SimpleNamespace(current_program_scene_name="Original")

    def get_scene_list(self):
        scenes = [{"sceneName": "Original"}]
        if self.scene_exists:
            scenes.append({"sceneName": CAPTURE_SCENE_NAME})
        return SimpleNamespace(scenes=scenes)

    def create_scene(self, name: str) -> None:
        self.calls.append(("create_scene", name))

    def get_input_list(self):
        inputs = [{"inputName": CAPTURE_INPUT_NAME}] if self.input_exists else []
        return SimpleNamespace(inputs=inputs)

    def create_input(self, scene, name, kind, settings, enabled):
        self.calls.append(("create_input", scene, name, kind, settings, enabled))
        return SimpleNamespace(scene_item_id=9)

    def set_input_settings(self, name, settings, overlay):
        self.calls.append(("set_input_settings", name, settings, overlay))

    def get_scene_item_id(self, scene, name):
        return SimpleNamespace(scene_item_id=9)

    def create_scene_item(self, scene, name, enabled):
        self.calls.append(("create_scene_item", scene, name, enabled))
        return SimpleNamespace(scene_item_id=9)

    def get_video_settings(self):
        return SimpleNamespace(base_width=1920, base_height=1080)

    def set_scene_item_transform(self, scene, item_id, transform):
        self.calls.append(("transform", scene, item_id, transform))

    def set_current_program_scene(self, name):
        self.calls.append(("scene", name))

    def get_source_screenshot(self, name, img_format, width, height, quality):
        self.calls.append(("screenshot", name, img_format, width, height, quality))
        return SimpleNamespace(image_data=self.screenshots.pop(0))


class FakeRecordingClient:
    def __init__(self) -> None:
        self.recording = False
        self.mute_states = {"Mic/Aux": False, "Muted microphone": True}
        self.calls: list[tuple] = []

    def get_input_list(self):
        return SimpleNamespace(
            inputs=[
                {"inputName": "Mic/Aux", "inputKind": "wasapi_input_capture"},
                {
                    "inputName": "Muted microphone",
                    "inputKind": "wasapi_input_capture_v2",
                },
                {"inputName": "Desktop Audio", "inputKind": "wasapi_output_capture"},
                {
                    "inputName": CAPTURE_AUDIO_INPUT_NAME,
                    "inputKind": APPLICATION_AUDIO_CAPTURE_KIND,
                },
            ]
        )

    def get_input_mute(self, name):
        return SimpleNamespace(input_muted=self.mute_states[name])

    def set_input_mute(self, name, muted):
        self.calls.append(("mute", name, muted))
        self.mute_states[name] = muted

    def set_record_directory(self, path):
        self.calls.append(("directory", path))

    def get_record_status(self):
        return SimpleNamespace(output_active=self.recording)

    def start_record(self):
        self.calls.append(("start",))
        self.recording = True

    def stop_record(self):
        self.calls.append(("stop",))
        self.recording = False


class RecoveryControl:
    def __init__(self, text: str) -> None:
        self.text = text
        self.clicked = False

    def window_text(self) -> str:
        return self.text

    def click_input(self) -> None:
        self.clicked = True


class RecoveryWindow:
    def __init__(self, controls: list[RecoveryControl]) -> None:
        self.controls = controls

    def window_text(self) -> str:
        return "OBS Studio"

    def descendants(self) -> list[RecoveryControl]:
        return self.controls


class RecoveryDesktop:
    def __init__(self, windows: list[RecoveryWindow]) -> None:
        self._windows = windows

    def windows(self, visible_only: bool) -> list[RecoveryWindow]:
        return self._windows


class ObsWindowCaptureTest(unittest.TestCase):
    @staticmethod
    def _image_data(color: tuple[int, int, int]) -> str:
        buffer = BytesIO()
        Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    def test_ensure_running_waits_for_previous_path_verified_obs_instance(self) -> None:
        controller = ObsController(ObsConfig(executable_path=r"D:\OBS\obs64.exe"))
        executable = Path(r"D:\OBS\obs64.exe")
        with patch.object(controller, "_is_websocket_port_open", return_value=False), patch(
            "video_agent.obs_controller.find_obs_executable", return_value=executable
        ), patch(
            "video_agent.obs_controller.wait_for_matching_processes_exit", return_value=True
        ) as wait, patch("video_agent.obs_controller.subprocess.Popen"), patch(
            "video_agent.obs_controller.time.sleep"
        ):
            controller.ensure_running()
        self.assertTrue(wait.called)

    def test_connect_waits_for_port_before_creating_obs_client(self) -> None:
        controller = ObsController(ObsConfig(startup_timeout_seconds=5))
        client = object()
        req_client = MagicMock(return_value=client)
        fake_module = SimpleNamespace(ReqClient=req_client)

        with patch.dict("sys.modules", {"obsws_python": fake_module}), patch.object(
            controller, "_is_websocket_port_open", side_effect=[False, False, True]
        ) as port_open, patch.object(
            controller, "_dismiss_recovery_dialog_if_present"
        ) as dismiss, patch(
            "video_agent.obs_controller.time.sleep"
        ) as sleep:
            controller.connect()

        self.assertIs(controller._client, client)
        self.assertEqual(port_open.call_count, 3)
        self.assertEqual(dismiss.call_count, 2)
        self.assertEqual(sleep.call_count, 2)
        req_client.assert_called_once_with(
            host="127.0.0.1", port=4455, password="", timeout=5
        )

    def test_connect_reports_clean_timeout_when_port_never_opens(self) -> None:
        controller = ObsController(ObsConfig(startup_timeout_seconds=1))
        req_client = MagicMock()
        fake_module = SimpleNamespace(ReqClient=req_client)

        with patch.dict("sys.modules", {"obsws_python": fake_module}), patch(
            "video_agent.obs_controller.time.monotonic", side_effect=[0, 0, 2]
        ), patch.object(
            controller, "_is_websocket_port_open", return_value=False
        ), patch.object(
            controller, "_dismiss_recovery_dialog_if_present"
        ), patch(
            "video_agent.obs_controller.time.sleep"
        ):
            with self.assertRaisesRegex(
                RuntimeError, "OBS WebSocket 127.0.0.1:4455 did not become ready within 1 seconds"
            ):
                controller.connect()

        req_client.assert_not_called()

    def test_recovery_dialog_chooses_normal_start_only_when_both_choices_exist(self) -> None:
        safe = RecoveryControl("以安全模式运行")
        normal = RecoveryControl("以正常模式运行")
        controller = ObsController(ObsConfig())

        self.assertTrue(
            controller._click_normal_start_on_recovery_dialog(
                RecoveryDesktop([RecoveryWindow([safe, normal])])
            )
        )
        self.assertTrue(normal.clicked)
        self.assertFalse(safe.clicked)

    def test_unrelated_normal_start_action_is_not_clicked(self) -> None:
        normal = RecoveryControl("正常启动")
        controller = ObsController(ObsConfig())

        self.assertFalse(
            controller._click_normal_start_on_recovery_dialog(
                RecoveryDesktop([RecoveryWindow([normal])])
            )
        )
        self.assertFalse(normal.clicked)
    def test_creates_dedicated_window_capture_scene_and_restores_original(self) -> None:
        controller = ObsController(ObsConfig())
        client = FakeObsClient()
        controller._client = client

        controller.configure_window_capture(
            CaptureTarget("钉钉视频会议", "QtWindowClass", "DingTalk.exe")
        )
        controller.restore_capture_scene()

        self.assertIn(("create_scene", CAPTURE_SCENE_NAME), client.calls)
        create = next(call for call in client.calls if call[0] == "create_input")
        self.assertEqual(create[3], "window_capture")
        self.assertEqual(create[4]["window"], "钉钉视频会议:QtWindowClass:DingTalk.exe")
        self.assertFalse(create[4]["cursor"])
        transform = next(call for call in client.calls if call[0] == "transform")[3]
        self.assertEqual(transform["boundsType"], "OBS_BOUNDS_SCALE_INNER")
        self.assertEqual((transform["boundsWidth"], transform["boundsHeight"]), (1920.0, 1080.0))
        self.assertEqual(
            (
                transform["cropLeft"],
                transform["cropTop"],
                transform["cropRight"],
                transform["cropBottom"],
            ),
            (0, 0, 0, 0),
        )
        self.assertEqual(client.calls[-2:], [("scene", CAPTURE_SCENE_NAME), ("scene", "Original")])

    def test_reuses_existing_scene_and_input(self) -> None:
        controller = ObsController(ObsConfig())
        client = FakeObsClient(scene_exists=True, input_exists=True)
        controller._client = client

        controller.configure_window_capture(CaptureTarget("Meeting", "Class", "DingTalk.exe"))

        self.assertFalse(any(call[0] == "create_scene" for call in client.calls))
        self.assertTrue(any(call[0] == "set_input_settings" for call in client.calls))
        transform = next(call for call in client.calls if call[0] == "transform")[3]
        self.assertEqual(
            {key: transform[key] for key in ("cropLeft", "cropTop", "cropRight", "cropBottom")},
            {"cropLeft": 0, "cropTop": 0, "cropRight": 0, "cropBottom": 0},
        )

    def test_creates_application_audio_capture_for_one_window(self) -> None:
        controller = ObsController(ObsConfig())
        client = FakeObsClient(scene_exists=True)
        controller._client = client

        controller.configure_application_audio_capture(
            CaptureTarget("抖音直播", "Chrome_WidgetWin_1", "chrome.exe")
        )

        create = next(call for call in client.calls if call[0] == "create_input")
        self.assertEqual(create[1:4], (CAPTURE_SCENE_NAME, CAPTURE_AUDIO_INPUT_NAME, APPLICATION_AUDIO_CAPTURE_KIND))
        self.assertEqual(create[4]["window"], "抖音直播:Chrome_WidgetWin_1:chrome.exe")

    def test_recording_mutes_only_microphones_and_restores_previous_states(self) -> None:
        controller = ObsController(ObsConfig(mute_microphone_during_recording=True))
        client = FakeRecordingClient()
        controller._client = client

        controller.start_recording(Path("test_outputs/obs/microphone-muted"))

        self.assertTrue(client.mute_states["Mic/Aux"])
        self.assertTrue(client.mute_states["Muted microphone"])
        self.assertNotIn(("mute", "Desktop Audio", True), client.calls)
        self.assertNotIn(("mute", CAPTURE_AUDIO_INPUT_NAME, True), client.calls)

        controller.stop_recording()

        self.assertFalse(client.mute_states["Mic/Aux"])
        self.assertTrue(client.mute_states["Muted microphone"])

    def test_microphone_muting_can_be_disabled_by_config(self) -> None:
        controller = ObsController(ObsConfig(mute_microphone_during_recording=False))
        client = FakeRecordingClient()
        controller._client = client

        controller.start_recording(Path("test_outputs/obs/microphone-allowed"))
        controller.stop_recording()

        self.assertFalse(any(call[0] == "mute" for call in client.calls))

    def test_recording_stop_uses_its_longer_dedicated_timeout(self) -> None:
        controller = ObsController(
            ObsConfig(startup_timeout_seconds=5, recording_stop_timeout_seconds=180)
        )
        controller._client = SimpleNamespace(
            get_record_status=lambda: SimpleNamespace(output_active=True)
        )

        with patch(
            "video_agent.obs_controller.time.monotonic", side_effect=[0, 0, 181]
        ), patch("video_agent.obs_controller.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "did not stop within 180 seconds"):
                controller._wait_for_recording_state(active=False)

    def test_capture_health_check_rejects_repeated_pure_black_frames(self) -> None:
        controller = ObsController(ObsConfig())
        client = FakeObsClient()
        client.screenshots = [self._image_data((0, 0, 0)) for _ in range(3)]
        controller._client = client
        diagnostic = Path("test_outputs/obs/black.png")

        visible = controller.verify_window_capture_visible(
            diagnostic,
            duration_seconds=0,
        )

        self.assertFalse(visible)
        self.assertTrue(diagnostic.exists())

    def test_capture_health_check_accepts_any_nonblack_frame(self) -> None:
        controller = ObsController(ObsConfig())
        client = FakeObsClient()
        client.screenshots = [
            self._image_data((0, 0, 0)),
            self._image_data((30, 30, 30)),
        ]
        controller._client = client

        self.assertTrue(
            controller.verify_window_capture_visible(
                Path("test_outputs/obs/unused.png"),
                duration_seconds=0,
            )
        )


if __name__ == "__main__":
    unittest.main()
