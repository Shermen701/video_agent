from __future__ import annotations

import unittest
from datetime import timedelta
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from video_agent.models import CaptureTarget, Credentials, utc_now
from video_agent.providers.mixlink import MixLinkProvider
from video_agent.tools.local_smoke_mixlink import _required_env


class FakeEdit:
    def __init__(self) -> None:
        self.values: list[str] = []

    def set_edit_text(self, value: str) -> None:
        self.values.append(value)


class FakeInfo:
    def __init__(self, automation_id: str) -> None:
        self.automation_id = automation_id


class FakeControl:
    def __init__(self, automation_id: str = "", text: str = "", visible: bool = True) -> None:
        self.element_info = FakeInfo(automation_id)
        self.text = text
        self.visible = visible
        self.clicked = False
        self.invoke_count = 0
        self.input_click_count = 0

    def window_text(self) -> str:
        return self.text

    def click_input(self) -> None:
        self.clicked = True
        self.input_click_count += 1

    def click(self) -> None:
        self.clicked = True
        self.invoke_count += 1

    def is_visible(self) -> bool:
        return self.visible


class PasswordEdit(FakeControl):
    def __init__(self) -> None:
        super().__init__()
        self.values: list[str] = []

    def set_edit_text(self, value: str) -> None:
        self.values.append(value)


class FakeWindow:
    def __init__(self, controls: list[FakeControl]) -> None:
        self.controls = controls

    def descendants(self, **_kwargs):
        return self.controls


class PromptWindow(FakeWindow):
    def __init__(self, controls: list[FakeControl], class_name: str = "PasswordPrompt") -> None:
        super().__init__(controls)
        self._class_name = class_name

    def class_name(self) -> str:
        return self._class_name

    def descendants(self, **kwargs):
        if kwargs.get("control_type") == "Edit":
            return [control for control in self.controls if isinstance(control, PasswordEdit)]
        return self.controls


class FakeTopWindow:
    def __init__(self, class_name: str, handle: int = 1) -> None:
        self._class_name = class_name
        self.handle = handle

    def class_name(self) -> str:
        return self._class_name

    def is_visible(self) -> bool:
        return True


class MixLinkProviderTest(unittest.TestCase):
    def test_set_edit_text_always_replaces_existing_value(self) -> None:
        edit = FakeEdit()
        MixLinkProvider._set_edit_text(edit, "current-account")
        self.assertEqual(edit.values, ["", "current-account"])

    def test_finds_real_join_container_by_stable_auto_id(self) -> None:
        join = FakeControl("root.video.joinWidget")
        window = FakeWindow([FakeControl("other"), join])
        self.assertIs(MixLinkProvider._find_by_auto_id_suffix(window, "joinWidget"), join)

    def test_finds_login_submit_by_stable_auto_id(self) -> None:
        submit = FakeControl("root.accountLoginPage.logInBtn")
        self.assertIs(MixLinkProvider._find_by_auto_id_suffix(FakeWindow([submit]), "logInBtn"), submit)

    def test_login_dialog_uses_same_retry_strategy_as_join_entry(self) -> None:
        login_entry = FakeControl("root.goLoginPshBtn")
        provider = MixLinkProvider({})
        provider.main_window = FakeWindow([login_entry])
        dialog = object()
        with patch.object(provider, "_wait_for_window_class", return_value=dialog):
            self.assertIs(provider._open_login_dialog(), dialog)
        self.assertTrue(login_entry.clicked)

    def test_login_dialog_falls_back_to_visible_login_text(self) -> None:
        login_entry = FakeControl(text="请登录 >")
        provider = MixLinkProvider({})
        provider.main_window = FakeWindow([login_entry])
        dialog = object()
        with patch.object(provider, "_wait_for_window_class", return_value=dialog):
            self.assertIs(provider._open_login_dialog(), dialog)
        self.assertTrue(login_entry.clicked)

    def test_login_agreement_falls_back_to_visible_agreement_text(self) -> None:
        agreement = FakeControl(text="我已阅读并同意用户协议")
        provider = MixLinkProvider({})

        provider._accept_login_agreement(FakeWindow([agreement]))

        self.assertEqual(agreement.input_click_count, 1)

    def test_checked_login_agreement_is_left_unchanged(self) -> None:
        agreement = FakeControl("root.sureChkbx.mnBtn")
        agreement.get_toggle_state = lambda: True  # type: ignore[attr-defined]
        provider = MixLinkProvider({})

        provider._accept_login_agreement(FakeWindow([agreement]))

        self.assertFalse(agreement.clicked)

    def test_post_join_preparation_mutes_mic_and_stops_active_video(self) -> None:
        microphone = FakeControl(text="静音")
        camera = FakeControl(text="停止视频")
        provider = MixLinkProvider({})
        provider.meeting_window = FakeWindow([microphone, camera])

        provider.prepare_audio_video()

        self.assertTrue(microphone.clicked)
        self.assertTrue(camera.clicked)
        self.assertEqual(microphone.invoke_count, 1)
        self.assertEqual(camera.invoke_count, 1)

    def test_post_join_preparation_does_not_enable_muted_devices(self) -> None:
        microphone = FakeControl(text="解除静音")
        camera = FakeControl(text="开启视频")
        provider = MixLinkProvider({})
        provider.meeting_window = FakeWindow([microphone, camera])

        provider.prepare_audio_video()

        self.assertFalse(microphone.clicked)
        self.assertFalse(camera.clicked)

    def test_finds_meeting_password_prompt_but_not_login_window(self) -> None:
        password_prompt = PromptWindow([PasswordEdit()])
        login = PromptWindow([PasswordEdit()], MixLinkProvider.LOGIN_CLASS)
        provider = MixLinkProvider({})
        with patch.object(provider, "_process_windows", return_value=[login, password_prompt]), patch.object(
            provider, "_texts", return_value=["请输入会议密码"]
        ):
            self.assertIs(provider._find_password_prompt(), password_prompt)

    def test_submits_meeting_password_with_invoke(self) -> None:
        edit = PasswordEdit()
        submit = FakeControl(text="确定")
        provider = MixLinkProvider({})

        provider._submit_meeting_password(PromptWindow([edit, submit]), "4444")

        self.assertEqual(edit.values, ["", "4444"])
        self.assertEqual(submit.invoke_count, 1)

    def test_waits_for_delayed_password_prompt_then_meeting_window(self) -> None:
        provider = MixLinkProvider({})
        prompt = object()
        meeting = object()
        with patch("video_agent.providers.mixlink.time.monotonic", return_value=0), patch(
            "video_agent.providers.mixlink.time.sleep"
        ), patch.object(provider, "_password_error_message", return_value=None), patch.object(
            provider, "_find_meeting_window", side_effect=[None, meeting]
        ), patch.object(provider, "_find_password_prompt", return_value=prompt), patch.object(
            provider, "_submit_meeting_password"
        ) as submit:
            self.assertIs(provider._wait_for_meeting_or_password("4444", 15), meeting)
        submit.assert_called_once_with(prompt, "4444")

    def test_password_rejection_fails_without_recording_password(self) -> None:
        provider = MixLinkProvider({})
        with patch("video_agent.providers.mixlink.time.monotonic", return_value=0), patch.object(
            provider, "_password_error_message", return_value="MixLink meeting password rejected"
        ), patch.object(provider, "_record_join_state") as record:
            with self.assertRaisesRegex(RuntimeError, "password rejected"):
                provider._wait_for_meeting_or_password("4444", 15)
        record.assert_called_once_with("password_rejected")

    def test_empty_meeting_password_does_not_query_prompt(self) -> None:
        provider = MixLinkProvider({})
        meeting = object()
        with patch("video_agent.providers.mixlink.time.monotonic", return_value=0), patch.object(
            provider, "_password_error_message", return_value=None), patch.object(
            provider, "_find_meeting_window", return_value=meeting
        ), patch.object(provider, "_find_password_prompt") as find_prompt:
            self.assertIs(provider._wait_for_meeting_or_password("", 15), meeting)
        find_prompt.assert_not_called()

    def test_capture_target_uses_ezmeeting_window(self) -> None:
        provider = MixLinkProvider({})
        provider.meeting_window = type("Window", (), {"handle": 12})()
        with patch.object(provider, "_is_window_visible", return_value=True), patch.object(
            provider, "_window_identity", return_value=("觅讯视频会议", "Qt5152QWindowIcon", "EZMeeting.exe")
        ):
            self.assertEqual(
                provider.get_capture_target(),
                CaptureTarget("觅讯视频会议", "Qt5152QWindowIcon", "EZMeeting.exe"),
            )

    def test_join_dialog_is_not_mistaken_for_meeting_window(self) -> None:
        provider = MixLinkProvider({})
        join = type("Window", (), {"handle": 8})()
        with patch.object(provider, "_process_windows", return_value=[join]), patch.object(
            provider, "_window_handle", return_value=8
        ), patch.object(
            provider,
            "_window_identity",
            return_value=("加入会议", "videoconference::JoinConfDialog", "EzEasyLink.exe"),
        ), patch.object(provider, "_texts", return_value=["结束会议"]):
            self.assertIsNone(provider._find_meeting_window())

    def test_prefers_room_widget_from_ezmeeting(self) -> None:
        provider = MixLinkProvider({})
        room = type("Window", (), {"handle": 9, "class_name": lambda self: "RoomWidget"})()
        with patch.object(provider, "_process_windows", return_value=[room]), patch.object(
            provider, "_window_handle", return_value=9
        ), patch.object(
            provider,
            "_window_identity",
            return_value=("视频会议", "Qt5152QWindowIcon", "EZMeeting.exe"),
        ):
            self.assertIs(provider._find_meeting_window(), room)

    def test_ignores_lingering_ezmeeting_auxiliary_window_after_meeting(self) -> None:
        provider = MixLinkProvider({})
        auxiliary = type("Window", (), {"handle": 10, "class_name": lambda self: "Qt5152QWindowIcon"})()
        with patch.object(provider, "_process_windows", return_value=[auxiliary]), patch.object(
            provider, "_window_handle", return_value=10
        ), patch.object(
            provider,
            "_window_identity",
            return_value=("", "Qt5152QWindowIcon", "EZMeeting.exe"),
        ):
            self.assertIsNone(provider._find_meeting_window())

    def test_recognizes_active_qt_meeting_window_by_native_title(self) -> None:
        provider = MixLinkProvider({})
        meeting = type("Window", (), {"handle": 10, "class_name": lambda self: "Qt5152QWindowIcon"})()
        with patch.object(provider, "_process_windows", return_value=[meeting]), patch.object(
            provider, "_window_handle", return_value=10
        ), patch.object(
            provider,
            "_window_identity",
            return_value=("视频会议", "Qt5152QWindowIcon", "EZMeeting.exe"),
        ), patch.object(provider, "_texts", return_value=[]):
            self.assertIs(provider._find_meeting_window(), meeting)

    def test_wait_for_join_window_returns_when_the_dialog_appears(self) -> None:
        provider = MixLinkProvider({})
        dialog = object()
        with patch.object(provider, "_find_join_window", side_effect=[None, dialog]), patch(
            "video_agent.providers.mixlink.time.sleep"
        ):
            self.assertIs(provider._wait_for_join_window(1), dialog)

    def test_join_dialog_retries_with_qt_invoke_when_input_click_does_not_open_it(self) -> None:
        card = FakeControl("root.joinWidget")
        provider = MixLinkProvider({})
        provider.main_window = FakeWindow([card])
        dialog = object()
        with patch.object(provider, "_wait_for_join_window", side_effect=[None, dialog]):
            self.assertIs(provider._open_join_dialog(), dialog)
        self.assertTrue(card.clicked)

    def test_shutdown_only_allows_configured_install_root(self) -> None:
        provider = MixLinkProvider({"executable_path": r"D:\Chint\MixLink\EzEasyLink.exe"})
        provider.executable = Path(r"D:\Chint\MixLink\EzEasyLink.exe")
        with patch("video_agent.providers.mixlink.shutdown_matching_processes") as shutdown:
            provider.shutdown_application()
        self.assertEqual(shutdown.call_args.kwargs["allowed_roots"], {Path(r"D:\Chint\MixLink")})
        self.assertEqual(shutdown.call_args.kwargs["executable_names"], provider.PROCESS_NAMES)

    def test_launch_waits_45_seconds_for_cold_start_by_default(self) -> None:
        provider = MixLinkProvider({"executable_path": r"D:\Chint\MixLink\EzEasyLink.exe"})
        executable = Path(r"D:\Chint\MixLink\EzEasyLink.exe")
        with patch.object(provider, "_ensure_pywinauto"), patch(
            "video_agent.providers.mixlink.find_mixlink_executable", return_value=executable
        ), patch.object(provider, "_record_startup_state"), patch.object(
            provider, "_connect_main_window", side_effect=[False, True]
        ) as connect, patch("video_agent.providers.mixlink.subprocess.Popen"):
            provider.launch()
        self.assertEqual(connect.call_args_list[0].args, (2,))
        self.assertEqual(connect.call_args_list[1].args, (45.0,))

    def test_main_window_class_is_accepted_without_process_identity(self) -> None:
        provider = MixLinkProvider({})
        main = FakeTopWindow(provider.MAIN_CLASS)
        with patch.object(provider, "_uia_windows", return_value=[main]), patch.object(
            provider, "_process_windows", return_value=[]
        ):
            self.assertIs(provider._find_window_by_class(provider.MAIN_CLASS), main)

    def test_window_identity_uses_native_path_query_without_psutil(self) -> None:
        win32gui = SimpleNamespace(
            GetWindowText=lambda _handle: "加入会议",
            GetClassName=lambda _handle: "videoconference::JoinConfDialog",
        )
        win32process = SimpleNamespace(GetWindowThreadProcessId=lambda _handle: (1, 321))
        with patch.dict(
            "sys.modules",
            {"win32gui": win32gui, "win32process": win32process, "psutil": None},
        ), patch(
            "video_agent.providers.mixlink._query_process_path",
            return_value=Path(r"D:\\Chint\\MixLink\\EzEasyLink.exe"),
        ) as query_path:
            self.assertEqual(
                MixLinkProvider._window_identity(99),
                ("加入会议", "videoconference::JoinConfDialog", "EzEasyLink.exe"),
            )
        query_path.assert_called_once_with(321)

    def test_process_windows_finds_join_and_meeting_windows_without_psutil(self) -> None:
        provider = MixLinkProvider({})
        join = FakeTopWindow("videoconference::JoinConfDialog", handle=10)
        room = FakeTopWindow("RoomWidget", handle=11)
        with patch.dict("sys.modules", {"psutil": None}), patch.object(
            provider, "_uia_windows", return_value=[join, room]
        ), patch.object(
            provider,
            "_window_identity",
            side_effect=[
                ("加入会议", "videoconference::JoinConfDialog", "EzEasyLink.exe"),
                ("视频会议", "Qt5152QWindowIcon", "EZMeeting.exe"),
            ],
        ):
            self.assertEqual(provider._process_windows(visible_only=True), [join, room])

    def test_destroyed_meeting_window_ends_recording_without_home_page(self) -> None:
        provider = MixLinkProvider({})
        provider.meeting_window = FakeTopWindow("RoomWidget", handle=12)
        with patch.object(provider, "_find_meeting_window", return_value=None), patch.object(
            provider, "_window_exists", return_value=False
        ), patch("video_agent.providers.mixlink.time.sleep") as sleep:
            provider.wait_until_finished(utc_now() + timedelta(minutes=1))
        sleep.assert_not_called()

    def test_meeting_end_page_ends_recording_while_window_still_exists(self) -> None:
        provider = MixLinkProvider({})
        ended = FakeTopWindow("RoomWidget", handle=12)
        with patch.object(provider, "_find_meeting_window", return_value=ended), patch.object(
            provider, "_texts", return_value=["会议已结束"]
        ), patch("video_agent.providers.mixlink.time.sleep") as sleep:
            provider.wait_until_finished(utc_now() + timedelta(minutes=1))
        sleep.assert_not_called()

    def test_rating_dialog_ends_recording_without_home_page_lookup(self) -> None:
        provider = MixLinkProvider({})
        with patch.object(provider, "_has_finished_dialog", return_value=True), patch.object(
            provider, "_find_meeting_window"
        ) as find_meeting, patch("video_agent.providers.mixlink.time.sleep") as sleep:
            provider.wait_until_finished(utc_now() + timedelta(minutes=1))
        find_meeting.assert_not_called()
        sleep.assert_not_called()

    def test_hidden_rating_template_is_not_treated_as_finished_dialog(self) -> None:
        hidden_rating = FakeControl(text="请评价本次会议体验", visible=False)
        self.assertFalse(MixLinkProvider._has_visible_text(FakeWindow([hidden_rating]), "请评价本次会议体验"))

    def test_visible_rating_dialog_is_treated_as_finished_dialog(self) -> None:
        rating = FakeControl(text="请评价本次会议体验", visible=True)
        self.assertTrue(MixLinkProvider._has_visible_text(FakeWindow([rating]), "请评价本次会议体验"))

    def test_startup_timeout_diagnostic_includes_observed_state(self) -> None:
        provider = MixLinkProvider({"executable_path": r"D:\Chint\MixLink\EzEasyLink.exe"})
        executable = Path(r"D:\Chint\MixLink\EzEasyLink.exe")

        def record(phase: str) -> None:
            provider._startup_observations.append(f"startup {phase}: processes=['EzEasyLink.exe pid=1']")

        with patch.object(provider, "_ensure_pywinauto"), patch(
            "video_agent.providers.mixlink.find_mixlink_executable", return_value=executable
        ), patch.object(provider, "_record_startup_state", side_effect=record), patch.object(
            provider, "_connect_main_window", side_effect=[False, False]
        ), patch("video_agent.providers.mixlink.subprocess.Popen"):
            with self.assertRaisesRegex(RuntimeError, "waiting 45 seconds"):
                provider.launch()
        with TemporaryDirectory() as directory:
            diagnostic = provider.capture_diagnostics(Path(directory))
            assert diagnostic is not None
            self.assertIn("EzEasyLink.exe pid=1", diagnostic.read_text(encoding="utf-8"))

    def test_local_smoke_requires_explicit_environment_credentials(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "VIDEO_AGENT_SMOKE_ACCOUNT"):
                _required_env("VIDEO_AGENT_SMOKE_ACCOUNT")


if __name__ == "__main__":
    unittest.main()
