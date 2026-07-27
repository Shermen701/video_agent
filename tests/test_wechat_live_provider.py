from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

from video_agent.models import Credentials, MeetingInfo, utc_now
from video_agent.providers.wechat_live import WeChatLiveProvider


class FakeElementInfo:
    def __init__(self, name: str = "", class_name: str = "Chrome_WidgetWin_1", control_type: str = "") -> None:
        self.name = name
        self.class_name = class_name
        self.automation_id = ""
        self.control_type = control_type


class FakeRect:
    left = 0
    top = 0
    right = 1000
    bottom = 800

    def width(self) -> int:
        return self.right - self.left

    def height(self) -> int:
        return self.bottom - self.top


class FakeControl:
    def __init__(self, text: str) -> None:
        self.text = text
        self.element_info = FakeElementInfo(text)
        self.clicked = False

    def window_text(self) -> str:
        return self.text

    def click_input(self) -> None:
        self.clicked = True

    def rectangle(self) -> FakeRect:
        return FakeRect()


class FakeWindow(FakeControl):
    def __init__(self, title: str = "微信", controls: list[FakeControl] | None = None) -> None:
        super().__init__(title)
        self.controls = controls or []
        self.visible = True

    def descendants(self) -> list[FakeControl]:
        return self.controls

    def is_visible(self) -> bool:
        return self.visible

    def capture_as_image(self):
        image = MagicMock()
        image.save = MagicMock()
        return image


class WeChatLiveProviderTest(unittest.TestCase):
    def test_launch_attaches_existing_wechat_without_starting_process(self) -> None:
        provider = WeChatLiveProvider({})
        window = FakeWindow()
        provider._ensure_pywinauto = MagicMock()  # type: ignore[method-assign]
        provider._find_main_window = MagicMock(return_value=window)  # type: ignore[method-assign]

        with patch("video_agent.providers.wechat_live.subprocess.Popen") as popen:
            provider.launch()

        self.assertIs(provider.window, window)
        popen.assert_not_called()
        self.assertIn("attached_to_existing_wechat", provider._diagnostics)

    def test_login_page_is_rejected_without_using_task_credentials(self) -> None:
        provider = WeChatLiveProvider({})
        provider.window = FakeWindow(controls=[FakeControl("扫码登录")])

        with self.assertRaisesRegex(RuntimeError, "existing logged-in session"):
            provider.ensure_logged_in(Credentials("account", "password"))

    def test_join_requires_search_command_access_method(self) -> None:
        provider = WeChatLiveProvider({})
        provider.window = FakeWindow()

        with self.assertRaisesRegex(RuntimeError, "accessMethod"):
            provider.join(MeetingInfo("", extra={"accessMethod": "直播链接", "searchCommand": "央视网"}))

    def test_join_rejects_empty_search_command(self) -> None:
        provider = WeChatLiveProvider({})
        provider.window = FakeWindow()

        with self.assertRaisesRegex(RuntimeError, "search command is empty"):
            provider.join(MeetingInfo("", extra={"accessMethod": "搜索口令"}))

    def test_find_followed_channel_scrolls_until_exact_nickname_is_visible(self) -> None:
        provider = WeChatLiveProvider({"follow_scroll_max_pages": 3})
        window = FakeWindow(controls=[FakeControl("其他账号")])
        provider.window = window

        def scroll(target) -> None:
            target.controls = [FakeControl("央视网")]

        with patch.object(provider, "_scroll_follow_list", side_effect=scroll):
            control = provider._find_followed_channel(window, "央视网")

        self.assertIsNotNone(control)
        assert control is not None
        self.assertEqual(control.window_text(), "央视网")

    def test_find_followed_channel_stops_when_page_repeats(self) -> None:
        provider = WeChatLiveProvider({"follow_scroll_max_pages": 3})
        window = FakeWindow(controls=[FakeControl("其他账号")])
        provider.window = window

        with patch.object(provider, "_scroll_follow_list"):
            self.assertIsNone(provider._find_followed_channel(window, "央视网"))

        self.assertIn("follow_list_reached_end", provider._diagnostics)

    def test_page_transition_matches_following_count_suffix(self) -> None:
        provider = WeChatLiveProvider({})
        window = FakeWindow(controls=[FakeControl("我关注的视频号(3)")])
        provider._find_video_window = MagicMock(return_value=window)  # type: ignore[method-assign]

        self.assertTrue(provider._wait_for_any_text(window, ("我关注的视频号",)))

    def test_icon_anchor_uses_window_edge_instead_of_window_ratio(self) -> None:
        provider = WeChatLiveProvider({})
        window = FakeWindow()

        with patch.object(provider, "_native_click") as click:
            self.assertTrue(provider._click_anchor(window, "profile_click_ratio"))

        click.assert_called_once_with(957, 94)

    def test_video_channel_uses_global_search_before_coordinate_fallback(self) -> None:
        provider = WeChatLiveProvider({})
        window = FakeWindow()
        window.type_keys = MagicMock()  # type: ignore[attr-defined]

        with patch.object(provider, "_click_required") as fallback:
            provider._search_and_open_video_channel(window)

        self.assertEqual(window.type_keys.call_args_list[0].args, ("^f",))
        self.assertEqual(window.type_keys.call_args_list[-1].args, ("{ENTER}",))
        fallback.assert_not_called()

    def test_follow_navigation_prefers_accessible_follow_text_over_anchor(self) -> None:
        provider = WeChatLiveProvider({})
        window = FakeWindow(controls=[FakeControl("关注")])
        provider._wait_for_any_text = MagicMock(side_effect=[False, True])  # type: ignore[method-assign]

        with patch.object(provider, "_click_ratio") as click_ratio:
            self.assertTrue(
                provider._click_control_until(
                    window,
                    "follow_click_ratio",
                    ("我关注的视频号",),
                    names=("关注",),
                )
            )

        self.assertTrue(window.controls[0].clicked)
        click_ratio.assert_not_called()

    def test_profile_navigation_prefers_structural_icon(self) -> None:
        provider = WeChatLiveProvider({})
        window = FakeWindow()
        icon = FakeControl("")
        provider._find_profile_icon = MagicMock(return_value=icon)  # type: ignore[method-assign]
        provider._wait_for_any_text = MagicMock(side_effect=[False, True])  # type: ignore[method-assign]

        self.assertTrue(
            provider._click_profile_icon_until(window, ("赞和收藏",))
        )

        self.assertTrue(icon.clicked)
        self.assertIn("profile_icon_clicked_by_structure", provider._diagnostics)

    def test_wait_for_live_refreshes_then_uses_live_room_window(self) -> None:
        provider = WeChatLiveProvider({"refresh_poll_seconds": 1})
        provider.window = FakeWindow()
        provider.video_window = provider.window
        provider.recording_deadline = utc_now() + timedelta(minutes=1)
        card = FakeControl("直播中")
        live_window = FakeWindow("微信直播")
        provider._find_exact_control = MagicMock(return_value=card)  # type: ignore[method-assign]
        provider._wait_for_any_text = MagicMock(return_value=True)  # type: ignore[method-assign]
        provider._wait_for_navigation = MagicMock()  # type: ignore[method-assign]
        provider._find_live_window = MagicMock(return_value=live_window)  # type: ignore[method-assign]

        provider._wait_for_live_and_enter()

        self.assertTrue(card.clicked)
        self.assertIs(provider.live_window, live_window)
        self.assertIn("live_room_ready", provider._diagnostics)

    def test_wait_for_live_fails_at_planned_deadline_when_no_live_is_found(self) -> None:
        provider = WeChatLiveProvider({})
        provider.window = FakeWindow()
        provider.recording_deadline = utc_now() - timedelta(seconds=1)

        with self.assertRaisesRegex(RuntimeError, "计划时间内未检测到直播"):
            provider._wait_for_live_and_enter()

    def test_wait_until_finished_stops_on_explicit_live_end_text(self) -> None:
        provider = WeChatLiveProvider({"end_poll_seconds": 1})
        provider.live_window = FakeWindow(controls=[FakeControl("本场直播已结束")])

        provider.wait_until_finished(utc_now() + timedelta(minutes=1))

        self.assertIn("live_end_detected", provider._diagnostics)

    def test_capture_target_uses_live_room_window(self) -> None:
        provider = WeChatLiveProvider({"capture_executable_name": "WeChatAppEx.exe"})
        provider.live_window = FakeWindow("微信直播", controls=[])

        target = provider.get_capture_target()

        self.assertEqual(target.title, "微信直播")
        self.assertEqual(target.class_name, "Chrome_WidgetWin_1")
        self.assertEqual(target.executable_name, "WeChatAppEx.exe")

    def test_shutdown_and_cleanup_leave_wechat_process_untouched(self) -> None:
        provider = WeChatLiveProvider({})
        provider.window = FakeWindow()
        provider.live_window = FakeWindow("微信直播")
        provider.search_command = "央视网"

        provider.shutdown_application()
        provider.cleanup()

        self.assertIn("wechat_left_running", provider._diagnostics)
        self.assertIsNone(provider.window)
        self.assertIsNone(provider.live_window)
        self.assertEqual(provider.search_command, "")

    def test_diagnostics_writes_text_report(self) -> None:
        provider = WeChatLiveProvider({})
        provider.window = FakeWindow(controls=[FakeControl("视频号")])
        output_dir = Path("test_outputs") / "wechat_diagnostic"

        path = provider.capture_diagnostics(output_dir)

        self.assertIsNotNone(path)
        assert path is not None
        self.assertIn("WeChat live diagnostics", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
