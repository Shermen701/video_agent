from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from video_agent.models import CaptureTarget, Credentials, MeetingInfo
from video_agent.providers.tencent_meeting import TencentMeetingProvider


class FakeRect:
    left = 100
    top = 200
    right = 300
    bottom = 240

    def width(self) -> int:
        return self.right - self.left

    def height(self) -> int:
        return self.bottom - self.top


class FakeWindow:
    def __init__(self, automation_id: str = "") -> None:
        self.element_info = SimpleNamespace(automation_id=automation_id)

    def texts(self) -> list[str]:
        return []

    def descendants(self, **_kwargs):
        return []


class FakeEdit:
    def __init__(self, value: str = "") -> None:
        self.value = value
        self.values: list[str] = []
        self.typed: list[str] = []
        self.clicked = False

    def set_edit_text(self, value: str) -> None:
        self.values.append(value)

    def window_text(self) -> str:
        return self.value

    def click_input(self) -> None:
        self.clicked = True

    def type_keys(self, value: str, **_kwargs) -> None:
        self.typed.append(value)


class FakeControl:
    def __init__(self, automation_id: str, rect: FakeRect | None = None) -> None:
        self.element_info = SimpleNamespace(automation_id=automation_id)
        self._rect = rect or FakeRect()

    def rectangle(self) -> FakeRect:
        return self._rect


class TencentMeetingProviderTest(unittest.TestCase):
    def test_capture_target_uses_the_current_tencent_meeting_window(self) -> None:
        provider = TencentMeetingProvider({})
        provider.meeting_window = SimpleNamespace(handle=123)
        target = CaptureTarget("腾讯会议", "BaseDialog", "WeMeetApp.exe")

        with patch.object(provider, "_window_is_available", return_value=True), patch.object(
            provider, "_capture_target_from_handle", return_value=target
        ) as capture:
            self.assertEqual(provider.get_capture_target(), target)

        capture.assert_called_once_with(123)

    def test_capture_target_refuses_an_invisible_tencent_meeting_window(self) -> None:
        provider = TencentMeetingProvider({})
        provider.meeting_window = SimpleNamespace(handle=123)

        with patch.object(provider, "_window_is_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "no longer visible"):
                provider.get_capture_target()

    def test_tencent_capture_health_check_defaults_to_five_seconds(self) -> None:
        self.assertEqual(TencentMeetingProvider({}).capture_health_check_seconds(), 5.0)
        self.assertEqual(TencentMeetingProvider({"capture_health_check_seconds": 0}).capture_health_check_seconds(), 0.0)

    def test_login_guide_is_recognized_before_text_finishes_loading(self) -> None:
        provider = TencentMeetingProvider({})
        provider.window = FakeWindow(TencentMeetingProvider.LOGIN_GUIDE_PAGE)

        self.assertTrue(provider._is_login_page())

    def test_agreement_prompt_handler_does_not_click_normal_agreement_text(self) -> None:
        provider = TencentMeetingProvider({})

        with patch.object(provider, "_text_rectangle", return_value=None), patch.object(
            provider, "_invoke_exact_button_if_present"
        ) as invoke, patch.object(provider, "_click_ratio") as click:
            provider._dismiss_agreement_prompt_if_present()

        invoke.assert_not_called()
        click.assert_not_called()

    def test_agreement_prompt_uses_accept_button_only_after_prompt_is_detected(self) -> None:
        provider = TencentMeetingProvider({})

        with patch.object(provider, "_text_rectangle", return_value=FakeRect()), patch.object(
            provider, "_invoke_exact_button_if_present", return_value=True
        ) as invoke, patch.object(provider, "_click_ratio") as click, patch("time.sleep"):
            provider._dismiss_agreement_prompt_if_present()

        invoke.assert_called_once_with("同意")
        click.assert_not_called()

    def test_agreement_prompt_prefers_structural_right_action(self) -> None:
        provider = TencentMeetingProvider({})
        action = FakeControl("QApplication.wemeet://page/nxui/alert.NXQtText:123")

        with patch.object(provider, "_find_agreement_accept_control", return_value=action), patch.object(
            provider, "_click_absolute"
        ) as click, patch.object(provider, "_click_ratio") as fallback, patch("time.sleep"):
            provider._dismiss_agreement_prompt_if_present()

        click.assert_called_once_with(200, 220)
        fallback.assert_not_called()

    def test_password_login_mode_is_selected_before_credentials(self) -> None:
        provider = TencentMeetingProvider({})

        with patch.object(provider, "_edit_controls", return_value=[]), patch.object(
            provider, "_has_text", return_value=False
        ), patch.object(
            provider, "_click_if_present", return_value=True
        ) as click, patch("time.sleep"):
            provider._select_password_login()

        click.assert_called_once_with(["密码登录", "手机密码登录"])

    def test_phone_login_prefers_structural_login_tile_over_ratio_fallback(self) -> None:
        provider = TencentMeetingProvider({})
        tile = FakeControl("root.NXQtHoverTipContainer:123")

        with patch.object(provider, "_click_if_present", return_value=False), patch.object(
            provider, "_find_phone_login_entry", return_value=tile
        ), patch.object(provider, "_click_absolute") as click, patch.object(provider, "_click_ratio") as fallback:
            provider._click_phone_login_if_present()

        click.assert_called_once_with(200, 220)
        fallback.assert_not_called()

    def test_login_submit_prefers_structural_action_over_ratio_fallback(self) -> None:
        provider = TencentMeetingProvider({})
        action = FakeControl("root.NXQtText:123")

        with patch.object(provider, "_click_if_present", return_value=False), patch.object(
            provider, "_find_login_submit_control", return_value=action
        ), patch.object(provider, "_click_absolute") as click, patch.object(provider, "_click_ratio") as fallback:
            provider._click_login()

        click.assert_called_once_with(200, 220)
        fallback.assert_not_called()

    def test_home_join_prefers_structural_quick_action_over_ratio_fallback(self) -> None:
        provider = TencentMeetingProvider({})
        provider.window = FakeWindow()
        action = FakeControl("root.NXQtImage:123")

        with patch.object(provider, "_find_home_join_entry", return_value=action), patch.object(
            provider, "_click_absolute"
        ) as click, patch.object(provider, "_native_click") as fallback:
            provider._click_home_join_fallback()

        click.assert_called_once_with(200, 220)
        fallback.assert_not_called()

    def test_home_settings_prefers_structural_sidebar_icon(self) -> None:
        provider = TencentMeetingProvider({})
        provider.window = FakeWindow()
        icon = FakeControl("root.NXQtImage:123")

        with patch.object(provider, "_find_home_settings_entry", return_value=icon), patch.object(
            provider, "_click_absolute"
        ) as click:
            provider._open_home_settings()

        click.assert_called_once_with(200, 220)

    def test_logout_uses_structural_bottom_account_action(self) -> None:
        provider = TencentMeetingProvider({})
        action = FakeControl("root.SettingPageContainer.NXQtText:123")

        with patch.object(provider, "_find_logout_control", return_value=action), patch.object(
            provider, "_click_absolute"
        ) as click:
            provider._click_logout_control(FakeWindow())

        click.assert_called_once_with(200, 220)

    def test_three_edit_phone_password_form_needs_no_mode_switch(self) -> None:
        provider = TencentMeetingProvider({})

        with patch.object(
            provider, "_edit_controls", return_value=[FakeEdit("86"), FakeEdit(), FakeEdit()]
        ), patch.object(provider, "_click_if_present") as click:
            provider._select_password_login()

        click.assert_not_called()

    def test_login_flow_waits_for_password_form_before_submitting(self) -> None:
        provider = TencentMeetingProvider({"login_retry_count": 1})
        events: list[str] = []

        with patch.object(provider, "_ensure_pywinauto"), patch.object(
            provider, "_connect_window"
        ), patch.object(provider, "_is_login_page", return_value=True), patch.object(
            provider, "_wait_for_login_guide_ready", side_effect=lambda: events.append("guide")
        ), patch.object(provider, "_click_phone_login_if_present", side_effect=lambda: events.append("phone")), patch.object(
            provider, "_wait_for_phone_login_form", side_effect=lambda: events.append("form")
        ), patch.object(provider, "_select_password_login", side_effect=lambda: events.append("password_mode")), patch.object(
            provider, "_wait_for_login_edits", side_effect=lambda: events.append("edits")
        ), patch.object(provider, "_fill_login_credentials", side_effect=lambda _credentials: events.append("fill")), patch.object(
            provider, "_click_login", side_effect=lambda: events.append("submit")
        ), patch.object(provider, "_wait_until_login_complete", return_value=True), patch("time.sleep"):
            provider.ensure_logged_in(Credentials("account", "password"))

        self.assertEqual(events, ["guide", "phone", "form", "password_mode", "edits", "fill", "submit"])

    def test_login_edit_uses_keyboard_events_for_form_validation(self) -> None:
        edit = FakeEdit()

        with patch("time.sleep"):
            TencentMeetingProvider._set_login_edit_text(edit, "value.")

        self.assertEqual(edit.values, [])
        self.assertTrue(edit.clicked)
        self.assertEqual(edit.typed, ["^a{BACKSPACE}", "value."])

    def test_transient_toast_is_not_treated_as_main_window(self) -> None:
        window = FakeWindow("QApplication.wemeet://page/nxui/toast")

        self.assertTrue(TencentMeetingProvider._is_transient_window(window))

    def test_transient_alert_is_not_treated_as_main_window(self) -> None:
        window = FakeWindow("QApplication.wemeet://page/nxui/alert")

        self.assertTrue(TencentMeetingProvider._is_transient_window(window))

    def test_login_guide_is_not_treated_as_transient_window(self) -> None:
        window = FakeWindow(TencentMeetingProvider.LOGIN_GUIDE_PAGE)

        self.assertFalse(TencentMeetingProvider._is_transient_window(window))

    def test_in_meeting_window_is_recognized_by_page_id(self) -> None:
        window = FakeWindow("QApplication.wemeet://page/inmeeting_revision")

        self.assertTrue(TencentMeetingProvider._is_in_meeting_window(window))

    def test_meeting_finishes_when_saved_meeting_window_is_destroyed(self) -> None:
        provider = TencentMeetingProvider({})
        provider.meeting_window = SimpleNamespace(handle=123)

        with patch.object(provider, "_window_exists", return_value=False), patch.object(
            provider, "_dismiss_after_meeting_dialog_if_present"
        ) as dismiss:
            self.assertTrue(provider._meeting_has_finished())

        dismiss.assert_called_once_with()

    def test_after_meeting_dialog_uses_its_exact_confirm_button(self) -> None:
        provider = TencentMeetingProvider({})
        button = SimpleNamespace(
            element_info=SimpleNamespace(
                automation_id=provider.AFTER_MEETING_CONFIRM_AUTOMATION_ID
            ),
            invoke=unittest.mock.Mock(),
        )

        with patch.object(provider, "_find_after_meeting_confirm_button", return_value=button):
            self.assertTrue(provider._dismiss_after_meeting_dialog_if_present())

        button.invoke.assert_called_once_with()

    def test_meeting_keeps_waiting_while_saved_meeting_window_exists(self) -> None:
        provider = TencentMeetingProvider({})
        provider.meeting_window = SimpleNamespace(handle=123)

        with patch.object(provider, "_window_exists", return_value=True), patch.object(
            provider, "_window_has_end_text", return_value=False
        ):
            self.assertFalse(provider._meeting_has_finished())

    def test_visible_end_text_finishes_before_window_is_destroyed(self) -> None:
        provider = TencentMeetingProvider({})
        provider.meeting_window = SimpleNamespace(handle=123)

        with patch.object(provider, "_window_exists", return_value=True), patch.object(
            provider, "_window_has_end_text", return_value=True
        ):
            self.assertTrue(provider._meeting_has_finished())

    def test_prepare_selects_computer_audio_before_audio_video_controls(self) -> None:
        provider = TencentMeetingProvider({})
        events: list[str] = []

        with patch.object(provider, "_ensure_pywinauto"), patch.object(
            provider, "_connect_in_meeting_window", side_effect=lambda **_kwargs: events.append("meeting")
        ), patch.object(
            provider, "_select_computer_audio_if_present", side_effect=lambda: events.append("audio")
        ), patch.object(
            provider, "_click_if_present", side_effect=lambda names: events.append(names[0])
        ):
            provider.prepare_audio_video()

        self.assertEqual(events, ["meeting", "audio", "静音", "关闭视频"])

    def test_computer_audio_prompt_is_optional(self) -> None:
        provider = TencentMeetingProvider({"computer_audio_prompt_timeout_seconds": 0.01})

        with patch.object(provider, "_invoke_exact_button_if_present", return_value=False) as click, patch("time.sleep"):
            provider._select_computer_audio_if_present()

        click.assert_called_with("使用电脑音频")

    def test_computer_audio_button_uses_invoke(self) -> None:
        provider = TencentMeetingProvider({})
        button = SimpleNamespace(
            window_text=lambda: "使用电脑音频",
            element_info=SimpleNamespace(control_type="Button"),
            is_visible=lambda: True,
            is_enabled=lambda: True,
            invoke=unittest.mock.Mock(),
        )
        provider.window = SimpleNamespace(descendants=lambda: [button])

        self.assertTrue(provider._invoke_exact_button_if_present("使用电脑音频"))
        button.invoke.assert_called_once_with()

    def test_click_first_prefers_exact_qt_button_invoke(self) -> None:
        provider = TencentMeetingProvider({})

        with patch.object(
            provider, "_invoke_exact_button_if_present", return_value=True
        ) as invoke, patch.object(provider, "_click_if_present") as click:
            provider._click_first(["加入会议", "入会"])

        invoke.assert_called_once_with("加入会议")
        click.assert_not_called()

    def test_home_join_never_uses_window_ratio_when_control_is_missing(self) -> None:
        provider = TencentMeetingProvider({})
        provider.window = SimpleNamespace(handle=1)

        with patch.object(provider, "_find_home_join_entry", return_value=None), patch(
            "video_agent.providers.tencent_meeting.time.monotonic", side_effect=[0, 0.5, 4]
        ), patch("video_agent.providers.tencent_meeting.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "join entry control did not appear"):
                provider._click_home_join_fallback()

    def test_home_join_accepts_dpi_scaled_quick_action_icon(self) -> None:
        provider = TencentMeetingProvider({})
        window = SimpleNamespace(
            rectangle=lambda: SimpleNamespace(left=100, top=200, right=2100, bottom=1400, width=lambda: 2000, height=lambda: 1200)
        )
        icon = FakeControl("root.NXQtImage:123", SimpleNamespace(left=420, top=500, right=564, bottom=644, width=lambda: 144, height=lambda: 144))
        window.descendants = lambda: [icon]
        provider.window = window

        self.assertIs(provider._find_home_join_entry(), icon)

    def test_shutdown_logs_out_before_closing_verified_processes(self) -> None:
        provider = TencentMeetingProvider({"executable_path": r"D:\Chint\WeMeet\WeMeetApp.exe"})

        with patch.object(provider, "_logout_current_account") as logout, patch(
            "video_agent.providers.tencent_meeting.find_tencent_meeting_executable",
            return_value=Path(r"D:\Chint\WeMeet\WeMeetApp.exe"),
        ), patch(
            "video_agent.providers.tencent_meeting.shutdown_matching_processes"
        ) as shutdown:
            provider.shutdown_application()

        logout.assert_called_once_with()
        shutdown.assert_called_once_with(
            executable_names={"WeMeetApp.exe"},
            allowed_roots={Path(r"D:\Chint\WeMeet")},
            timeout_seconds=5.0,
        )

    def test_join_connects_join_dialog_before_filling_meeting_number(self) -> None:
        provider = TencentMeetingProvider({"join_retry_count": 1})
        events: list[str] = []

        with patch.object(provider, "_ensure_pywinauto"), patch.object(
            provider, "_connect_window"
        ), patch.object(provider, "_click_if_present", return_value=False), patch.object(
            provider, "_click_home_join_fallback", side_effect=lambda: events.append("open")
        ), patch.object(
            provider, "_connect_join_window", side_effect=lambda **_kwargs: events.append("dialog") or True
        ), patch.object(provider, "_is_login_page", return_value=False), patch.object(
            provider, "_has_text", return_value=False
        ), patch.object(
            provider, "_fill_first_edit", side_effect=lambda value: events.append(f"fill:{value}")
        ), patch.object(provider, "_click_first", side_effect=lambda _names: events.append("submit")), patch.object(
            provider, "_wait_for_meeting_after_submit", side_effect=lambda _meeting: events.append("meeting") or True
        ), patch(
            "time.sleep"
        ):
            provider.join(MeetingInfo("274684226", "4444"))

        self.assertEqual(events, ["open", "dialog", "fill:274684226", "submit", "meeting"])

    def test_password_prompt_fills_dedicated_password_edit_and_submits(self) -> None:
        provider = TencentMeetingProvider({})
        password_edit = FakeEdit()
        events: list[str] = []

        with patch.object(
            provider, "_connect_in_meeting_window", side_effect=[False, True]
        ), patch.object(provider, "_connect_join_window", return_value=True), patch.object(
            provider, "_has_text", return_value=False
        ), patch.object(provider, "_meeting_password_edit", return_value=password_edit), patch.object(
            provider, "_set_login_edit_text", side_effect=lambda _edit, value: events.append(f"password:{value}")
        ), patch.object(provider, "_click_first", side_effect=lambda names: events.append(names[0])), patch(
            "time.sleep"
        ):
            self.assertTrue(provider._wait_for_meeting_after_submit(MeetingInfo("274684226", "4444")))

        self.assertEqual(events, ["password:4444", "加入"])

    def test_password_prompt_fails_clearly_when_password_is_missing(self) -> None:
        provider = TencentMeetingProvider({})

        with patch.object(provider, "_connect_in_meeting_window", return_value=False), patch.object(
            provider, "_connect_join_window", return_value=True
        ), patch.object(provider, "_has_text", return_value=False), patch.object(
            provider, "_meeting_password_edit", return_value=FakeEdit()
        ):
            with self.assertRaisesRegex(RuntimeError, "requires a meeting password"):
                provider._wait_for_meeting_after_submit(MeetingInfo("274684226"))


if __name__ == "__main__":
    unittest.main()
