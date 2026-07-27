from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import call, patch

from video_agent.models import CaptureTarget, Credentials
from video_agent.providers.dingtalk import DingTalkProvider


class FakeButton:
    def __init__(self, visible: bool) -> None:
        self.visible = visible

    def is_visible(self) -> bool:
        return self.visible


class ToggleControl:
    def __init__(self, text: str) -> None:
        self.text = text
        self.clicked = False

    def window_text(self) -> str:
        return self.text

    def click_input(self) -> None:
        self.clicked = True


class ToggleWindow:
    def __init__(self, *texts: str) -> None:
        self.controls = [ToggleControl(text) for text in texts]

    def descendants(self) -> list[ToggleControl]:
        return self.controls


class LoginEdit:
    def __init__(self, value: str) -> None:
        self.value = value


class LoginWindow:
    def __init__(self, account: str) -> None:
        self.mobile = LoginEdit(account)
        self.password = LoginEdit("")

    def child_window(self, auto_id: str) -> LoginEdit:
        if auto_id.endswith("editMobile"):
            return self.mobile
        if auto_id.endswith("editPassword"):
            return self.password
        raise RuntimeError("unknown login control")


class FakeWindow:
    def __init__(
        self, handle: int, prepare: bool, ended: bool = False, in_meeting: bool = False
    ) -> None:
        self.handle = handle
        self.prepare = prepare
        self.ended = ended
        self.in_meeting = in_meeting

    def child_window(self, auto_id: str):
        if not self.prepare:
            raise RuntimeError("control not present")
        return FakeButton(True)

    def texts(self) -> list[str]:
        if self.ended:
            return ["会议已结束"]
        if self.in_meeting:
            return ["会议信息", "成员", "共享", "结束"]
        return []

    def descendants(self) -> list[object]:
        return []


class PasswordEdit:
    def __init__(self) -> None:
        self.element_info = type("Info", (), {"control_type": "Edit", "class_name": ""})()
        self.values: list[str] = []

    def set_edit_text(self, value: str) -> None:
        self.values.append(value)

    def window_text(self) -> str:
        return ""


class PasswordControl:
    def __init__(self, text: str) -> None:
        self.text = text
        self.clicked = False
        self.element_info = type("Info", (), {"control_type": "Button", "class_name": ""})()

    def window_text(self) -> str:
        return self.text

    def click_input(self) -> None:
        self.clicked = True


class PasswordPrompt:
    def __init__(self, texts: list[str], controls: list[object]) -> None:
        self._texts = texts
        self._controls = controls

    def texts(self) -> list[str]:
        return self._texts

    def descendants(self) -> list[object]:
        return self._controls

    def child_window(self, auto_id: str) -> object:
        raise RuntimeError(f"unknown control: {auto_id}")


class EmbeddedPasswordPrompt(PasswordPrompt):
    def __init__(self, password_edit: PasswordEdit, meeting_edit: PasswordEdit, submit: PasswordControl) -> None:
        super().__init__(["会议密码"], [password_edit, meeting_edit, submit])
        self.password_edit = password_edit

    def child_window(self, auto_id: str) -> object:
        if auto_id.endswith("FlexHintDialog.main_input"):
            return self.password_edit
        raise RuntimeError(f"unknown control: {auto_id}")


class DingTalkProviderTest(unittest.TestCase):
    def test_join_card_uses_anchor_relative_to_meeting_webview(self) -> None:
        provider = DingTalkProvider({"home_join_webview_anchor": [0.31, 0.21]})
        rect = type(
            "Rect",
            (), {"left": 400, "top": 200, "right": 1400, "bottom": 1000, "width": lambda _self: 1000, "height": lambda _self: 800},
        )()
        webview = type(
            "WebView",
            (), {
                "element_info": type("Info", (), {"automation_id": "browser_window", "class_name": "client_ding::WebBrowserViewV2"})(),
                "rectangle": lambda _self: rect,
            },
        )()
        provider.window = type("Window", (), {"descendants": lambda _self: [webview]})()

        with patch.object(provider, "_bring_window_to_front"), patch.object(
            provider, "_native_click"
        ) as click, patch("video_agent.providers.dingtalk.time.sleep"):
            provider._click_join_meeting_card()

        click.assert_called_once_with(710, 368)

    def test_join_card_fails_when_meeting_webview_is_missing(self) -> None:
        provider = DingTalkProvider({})
        provider.window = type("Window", (), {"descendants": lambda _self: []})()

        with self.assertRaisesRegex(RuntimeError, "WebView control not found"):
            provider._click_join_meeting_card()

    def test_meeting_navigation_fails_instead_of_using_a_window_ratio(self) -> None:
        provider = DingTalkProvider({})
        provider.window = type(
            "Window",
            (), {"child_window": lambda _self, **_kwargs: type("Nav", (), {"children": lambda _nav: []})()},
        )()

        with self.assertRaisesRegex(RuntimeError, "meeting navigation control not found"):
            provider._navigate_to_meeting_home()

    def test_login_submit_prefers_its_auto_id_over_window_ratio(self) -> None:
        provider = DingTalkProvider({})

        with patch.object(provider, "_click_auto_id_if_present", return_value=True) as auto_id, patch.object(
            provider, "_click_ratio"
        ) as ratio:
            provider._click_login()

        auto_id.assert_called_once()
        ratio.assert_not_called()

    def test_login_submit_fails_instead_of_guessing_a_ratio(self) -> None:
        provider = DingTalkProvider({})

        with patch.object(provider, "_click_auto_id_if_present", return_value=False), patch.object(
            provider, "_click_if_present", return_value=False
        ), self.assertRaisesRegex(RuntimeError, "login submit control not found"):
            provider._click_login()

    def test_account_password_login_fails_instead_of_guessing_a_ratio(self) -> None:
        provider = DingTalkProvider({})

        with patch.object(provider, "_click_auto_id_if_present", return_value=False), patch.object(
            provider, "_click_if_present", return_value=False
        ), self.assertRaisesRegex(RuntimeError, "account password login control not found"):
            provider._click_account_password_login_if_present()

    def test_launch_uses_dedicated_dingtalk_startup_timeout(self) -> None:
        provider = DingTalkProvider({"startup_timeout_seconds": 90})
        with patch.object(
            provider, "_connect_window", side_effect=[RuntimeError("not running"), None]
        ) as connect, patch(
            "video_agent.providers.dingtalk.find_dingtalk_executable",
            return_value=Path("D:/DingDing/DingtalkLauncher.exe"),
        ), patch("video_agent.providers.dingtalk.subprocess.Popen"):
            provider.launch()
        self.assertEqual(
            connect.call_args_list, [call(timeout_seconds=3), call(timeout_seconds=90)]
        )

    def test_login_replaces_a_different_remembered_account(self) -> None:
        self._assert_login_fields_rewritten("old-account")

    def test_login_rewrites_the_same_remembered_account(self) -> None:
        self._assert_login_fields_rewritten("task-account")

    def test_login_fills_an_empty_account(self) -> None:
        self._assert_login_fields_rewritten("")

    def _assert_login_fields_rewritten(self, remembered_account: str) -> None:
        provider = DingTalkProvider({})
        window = LoginWindow(remembered_account)
        provider.window = window
        credentials = Credentials("task-account", "task-password")

        with patch.object(provider, "_set_edit_text") as set_edit_text:
            provider._fill_login_credentials(credentials)

        self.assertEqual(
            set_edit_text.call_args_list,
            [
                call(window.mobile, "task-account"),
                call(window.password, "task-password"),
            ],
        )

    def test_audio_video_preparation_only_clicks_actions_that_turn_devices_off(self) -> None:
        provider = DingTalkProvider({})
        window = ToggleWindow("静音", "关闭摄像头")
        provider.meeting_window = window

        with patch.object(provider, "_ensure_pywinauto"):
            provider.prepare_audio_video()

        self.assertEqual([item.clicked for item in window.controls], [True, True])

    def test_audio_video_preparation_does_not_enable_disabled_devices(self) -> None:
        provider = DingTalkProvider({})
        window = ToggleWindow("解除静音", "开摄像头", "开启摄像头")
        provider.meeting_window = window

        with patch.object(provider, "_ensure_pywinauto"):
            provider.prepare_audio_video()

        self.assertFalse(any(item.clicked for item in window.controls))

    def test_selects_meeting_window_when_same_title_prepare_window_also_exists(self) -> None:
        prepare = FakeWindow(1, prepare=True)
        meeting = FakeWindow(2, prepare=False)

        selected = DingTalkProvider._select_titled_window(
            [prepare, meeting], expect_prepare=False
        )

        self.assertIs(selected, meeting)

    def test_reuses_submitted_prepare_window_handle_even_if_uia_button_is_stale(self) -> None:
        reused = FakeWindow(1, prepare=True)
        other = FakeWindow(2, prepare=False)

        selected = DingTalkProvider._select_titled_window(
            [other, reused], expect_prepare=None, preferred_handle=1
        )

        self.assertIs(selected, reused)

    def test_exposes_saved_meeting_window_as_capture_target(self) -> None:
        provider = DingTalkProvider({})
        provider.meeting_window = FakeWindow(20, prepare=False)
        target = CaptureTarget("钉钉视频会议", "QtClass", "DingTalk.exe")

        with patch.object(provider, "_window_is_available", return_value=True), patch.object(
            provider, "_capture_target_from_handle", return_value=target
        ):
            self.assertEqual(provider.get_capture_target(), target)

    def test_recognizes_window_that_is_already_in_meeting(self) -> None:
        window = FakeWindow(20, prepare=False, in_meeting=True)

        self.assertTrue(DingTalkProvider._is_in_meeting_window(window))

    def test_meeting_end_requires_closed_window_and_home_end_state(self) -> None:
        provider = DingTalkProvider({})
        provider.meeting_window = FakeWindow(20, prepare=False)

        with patch.object(provider, "_window_is_available", return_value=False), patch.object(
            provider, "_find_meeting_window", return_value=None
        ), patch.object(provider, "_connect_window"), patch.object(
            provider, "_is_login_page", return_value=False
        ), patch.object(provider, "_has_text", return_value=True):
            self.assertTrue(provider._meeting_has_finished())

    def test_non_meeting_saved_window_allows_home_end_confirmation(self) -> None:
        provider = DingTalkProvider({})
        provider.meeting_window = FakeWindow(20, prepare=False)

        with patch.object(provider, "_window_is_available", return_value=True), patch.object(
            provider, "_find_meeting_window", return_value=FakeWindow(21, prepare=False)
        ), patch.object(provider, "_connect_window"), patch.object(
            provider, "_is_login_page", return_value=False
        ), patch.object(provider, "_has_text", return_value=True):
            self.assertTrue(provider._meeting_has_finished())

    def test_end_text_in_reused_meeting_window_finishes_immediately(self) -> None:
        provider = DingTalkProvider({})
        provider.meeting_window = FakeWindow(20, prepare=False, ended=True)

        with patch.object(provider, "_window_is_available", return_value=True):
            self.assertTrue(provider._meeting_has_finished())

    def test_transient_window_lookup_failure_does_not_end_meeting(self) -> None:
        provider = DingTalkProvider({})
        provider.meeting_window = FakeWindow(20, prepare=False)

        with patch.object(provider, "_window_is_available", return_value=False), patch.object(
            provider, "_find_meeting_window", return_value=None
        ), patch.object(provider, "_connect_window", side_effect=RuntimeError("uia busy")):
            self.assertFalse(provider._meeting_has_finished())

    def test_two_missing_meeting_window_polls_end_when_client_is_gone(self) -> None:
        provider = DingTalkProvider({})
        provider.meeting_window = FakeWindow(20, prepare=False)

        with patch.object(provider, "_window_is_available", return_value=False), patch.object(
            provider, "_find_meeting_window", return_value=None
        ), patch.object(provider, "_connect_window", side_effect=RuntimeError("client closed")):
            self.assertFalse(provider._meeting_has_finished())
            self.assertTrue(provider._meeting_has_finished())

    def test_finds_meeting_password_prompt_and_ignores_unrelated_window(self) -> None:
        edit = PasswordEdit()
        prompt = PasswordPrompt(["请输入会议密码"], [edit])
        other = PasswordPrompt(["加入会议"], [PasswordEdit()])
        provider = DingTalkProvider({})

        with patch.object(provider, "_visible_titled_meeting_windows", return_value=[other, prompt]):
            self.assertIs(provider._find_password_prompt(), prompt)

    def test_submits_meeting_password_without_leaking_it(self) -> None:
        edit = PasswordEdit()
        submit = PasswordControl("确定")
        provider = DingTalkProvider({})

        provider._submit_meeting_password(PasswordPrompt(["会议密码"], [edit, submit]), "4444")

        self.assertEqual(edit.values, ["", "4444"])
        self.assertTrue(submit.clicked)

    def test_embedded_password_prompt_does_not_overwrite_meeting_number(self) -> None:
        password_edit = PasswordEdit()
        meeting_number_edit = PasswordEdit()
        submit = PasswordControl("确定")
        provider = DingTalkProvider({})

        provider._submit_meeting_password(
            EmbeddedPasswordPrompt(password_edit, meeting_number_edit, submit), "4444"
        )

        self.assertEqual(password_edit.values, ["", "4444"])
        self.assertEqual(meeting_number_edit.values, [])

    def test_waits_for_password_prompt_then_meeting_window(self) -> None:
        provider = DingTalkProvider({})
        prompt = object()
        meeting = FakeWindow(2, prepare=False, in_meeting=True)
        with patch("video_agent.providers.dingtalk.time.monotonic", return_value=0), patch(
            "video_agent.providers.dingtalk.time.sleep"
        ), patch.object(provider, "_password_error_message", return_value=None), patch.object(
            provider, "_find_password_prompt", return_value=prompt
        ), patch.object(provider, "_submit_meeting_password") as submit, patch.object(
            provider, "_find_meeting_window", return_value=meeting
        ):
            self.assertIs(provider._wait_for_meeting_or_password("4444", 15), meeting)
        submit.assert_called_once_with(prompt, "4444")

    def test_password_rejection_is_reported(self) -> None:
        provider = DingTalkProvider({})
        with patch("video_agent.providers.dingtalk.time.monotonic", return_value=0), patch.object(
            provider, "_password_error_message", return_value="DingTalk meeting password rejected"
        ):
            with self.assertRaisesRegex(RuntimeError, "password rejected"):
                provider._wait_for_meeting_or_password("4444", 15)

    def test_empty_password_does_not_query_password_prompt(self) -> None:
        provider = DingTalkProvider({})
        meeting = FakeWindow(2, prepare=False, in_meeting=True)
        with patch("video_agent.providers.dingtalk.time.monotonic", return_value=0), patch.object(
            provider, "_password_error_message", return_value=None
        ), patch.object(provider, "_find_meeting_window", return_value=meeting), patch.object(
            provider, "_find_password_prompt"
        ) as find_prompt:
            self.assertIs(provider._wait_for_meeting_or_password("", 15), meeting)
        find_prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
