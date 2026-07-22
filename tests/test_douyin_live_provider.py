from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from video_agent.models import MeetingInfo, utc_now
from video_agent.providers.douyin_live import DouyinLiveProvider


class DouyinLiveProviderTest(unittest.TestCase):
    def test_rejects_non_douyin_live_url(self) -> None:
        provider = DouyinLiveProvider({})

        with self.assertRaisesRegex(RuntimeError, "Douyin live-room URL"):
            provider._live_url(MeetingInfo("https://example.com/live"))

    def test_uses_platform_live_url_before_meeting_number(self) -> None:
        provider = DouyinLiveProvider({})

        url = provider._live_url(
            MeetingInfo("123", extra={"liveUrl": "https://live.douyin.com/64300823902"})
        )

        self.assertEqual(url, "https://live.douyin.com/64300823902")

    def test_join_uses_isolated_profile_and_waits_for_playback_before_fullscreen(self) -> None:
        executable = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
        provider = DouyinLiveProvider({"profile_dir": r"test_outputs\DouyinProfile"})
        provider.executable = executable
        provider._wait_for_playback = MagicMock()  # type: ignore[method-assign]
        provider._verify_player_ready = MagicMock()  # type: ignore[method-assign]
        provider._set_capture_title = MagicMock()  # type: ignore[method-assign]
        provider._wait_for_chrome_window = MagicMock(return_value=object())  # type: ignore[method-assign]

        with patch("video_agent.providers.douyin_live.subprocess.Popen") as popen:
            provider.join(MeetingInfo("https://live.douyin.com/64300823902"))

        command = popen.call_args.args[0]
        self.assertIn("--user-data-dir=test_outputs\\DouyinProfile", command)
        self.assertIn("--remote-debugging-port=9229", command)
        self.assertIn("--app=https://live.douyin.com/64300823902", command)
        provider._wait_for_playback.assert_called_once_with(45.0)
        provider._verify_player_ready.assert_called_once_with(45.0)

    def test_playback_wait_rejects_login_or_verification_page(self) -> None:
        provider = DouyinLiveProvider({})
        with patch.object(provider, "_page_state", return_value={"playing": False, "ended": False, "blocked": True}):
            with self.assertRaisesRegex(RuntimeError, "requires login or verification"):
                provider._wait_for_playback(10)

    def test_playback_wait_rejects_live_that_already_ended(self) -> None:
        provider = DouyinLiveProvider({})
        with patch.object(provider, "_try_start_playback"), patch.object(
            provider,
            "_page_state",
            return_value={"playing": False, "ended": True, "blocked": False},
        ):
            with self.assertRaisesRegex(RuntimeError, "meeting_join_failed.*already ended"):
                provider._wait_for_playback(10)

    def test_finished_page_waits_configured_tail_before_stopping(self) -> None:
        provider = DouyinLiveProvider({"end_delay_seconds": 5})
        deadline = utc_now() + timedelta(minutes=1)
        with patch.object(provider, "_maintain_capture_title") as maintain, patch.object(provider, "_collapse_chatroom") as collapse, patch.object(provider, "_page_state", return_value={"playing": True, "ended": True, "blocked": False}), patch(
            "video_agent.providers.douyin_live.time.sleep"
        ) as sleep:
            provider.wait_until_finished(deadline)

        maintain.assert_called_once()
        collapse.assert_called_once_with(required=False)
        sleep.assert_called_once_with(5.0)
        self.assertIn("live_end_signal=page", provider._diagnostics)

    def test_media_ended_signal_stops_with_tail_delay(self) -> None:
        provider = DouyinLiveProvider({"end_delay_seconds": 5})
        state = {"playing": False, "ended": True, "mediaEnded": True, "blocked": False}
        with patch.object(provider, "_maintain_capture_title"), patch.object(
            provider, "_collapse_chatroom"
        ), patch.object(provider, "_page_state", return_value=state), patch(
            "video_agent.providers.douyin_live.time.sleep"
        ) as sleep:
            provider.wait_until_finished(utc_now() + timedelta(minutes=1))

        sleep.assert_called_once_with(5.0)
        self.assertIn("live_end_signal=media", provider._diagnostics)

    def test_stalled_stream_for_thirty_seconds_stops_recording(self) -> None:
        provider = DouyinLiveProvider({"end_poll_seconds": 5, "end_delay_seconds": 5})
        state = {
            "playing": False,
            "ended": False,
            "blocked": False,
            "currentTime": 12.0,
            "streamInactive": True,
        }
        with patch.object(provider, "_maintain_capture_title"), patch.object(
            provider, "_collapse_chatroom"
        ), patch.object(provider, "_page_state", return_value=state), patch(
            "video_agent.providers.douyin_live.time.monotonic",
            side_effect=[0, 10, 20, 30, 31],
        ), patch("video_agent.providers.douyin_live.time.sleep") as sleep:
            provider.wait_until_finished(utc_now() + timedelta(minutes=1))

        self.assertIn("live_end_signal=stream_stalled_30s", provider._diagnostics)
        self.assertEqual(sleep.call_args.args[0], 5.0)

    def test_playback_progress_resets_stall_timer(self) -> None:
        provider = DouyinLiveProvider({"end_poll_seconds": 5})
        states = [
            {"ended": False, "blocked": False, "currentTime": 10.0, "streamInactive": True},
            {"ended": False, "blocked": False, "currentTime": 10.0, "streamInactive": True},
            {"ended": False, "blocked": False, "currentTime": 11.0, "streamInactive": False},
            {"ended": True, "blocked": False, "mediaEnded": False},
        ]
        with patch.object(provider, "_maintain_capture_title"), patch.object(
            provider, "_collapse_chatroom"
        ), patch.object(provider, "_page_state", side_effect=states), patch(
            "video_agent.providers.douyin_live.time.monotonic", return_value=0
        ), patch("video_agent.providers.douyin_live.time.sleep"):
            provider.wait_until_finished(utc_now() + timedelta(minutes=1))

        self.assertNotIn("live_end_signal=stream_stalled_30s", provider._diagnostics)
        self.assertIn("live_end_signal=page", provider._diagnostics)

    def test_two_page_state_misses_are_tolerated(self) -> None:
        provider = DouyinLiveProvider({"end_poll_seconds": 5})
        states = [None, None, {"ended": True, "blocked": False, "mediaEnded": False}]
        with patch.object(provider, "_maintain_capture_title"), patch.object(
            provider, "_collapse_chatroom"
        ), patch.object(provider, "_page_state", side_effect=states), patch(
            "video_agent.providers.douyin_live.time.sleep"
        ):
            provider.wait_until_finished(utc_now() + timedelta(minutes=1))

    def test_three_page_state_misses_fail(self) -> None:
        provider = DouyinLiveProvider({"end_poll_seconds": 5})
        with patch.object(provider, "_maintain_capture_title"), patch.object(
            provider, "_collapse_chatroom"
        ), patch.object(provider, "_page_state", return_value=None), patch(
            "video_agent.providers.douyin_live.time.sleep"
        ):
            with self.assertRaisesRegex(RuntimeError, "no longer reachable"):
                provider.wait_until_finished(utc_now() + timedelta(minutes=1))

    def test_page_state_preserves_media_progress_fields(self) -> None:
        provider = DouyinLiveProvider({})
        provider._evaluate = MagicMock(  # type: ignore[method-assign]
            return_value={
                "playing": False,
                "ended": True,
                "explicitEnded": False,
                "mediaEnded": True,
                "blocked": False,
                "paused": True,
                "readyState": 2,
                "networkState": 3,
                "currentTime": 18.5,
                "bufferedAhead": 0.0,
                "streamInactive": True,
            }
        )

        state = provider._page_state()

        assert state is not None
        self.assertTrue(state["mediaEnded"])
        self.assertEqual(state["currentTime"], 18.5)
        self.assertEqual(state["networkState"], 3)
        expression = provider._evaluate.call_args.args[0]
        self.assertIn("video.ended", expression)
        self.assertIn("video.currentTime", expression)
        self.assertIn("bufferedAhead", expression)

    def test_plan_deadline_remains_hard_stop(self) -> None:
        provider = DouyinLiveProvider({})
        provider._page_state = MagicMock()  # type: ignore[method-assign]

        provider.wait_until_finished(utc_now() - timedelta(seconds=1))

        provider._page_state.assert_not_called()

    def test_waiting_page_fails_when_login_is_requested(self) -> None:
        provider = DouyinLiveProvider({})
        with patch.object(provider, "_collapse_chatroom"), patch.object(provider, "_page_state", return_value={"playing": True, "ended": False, "blocked": True}):
            with self.assertRaisesRegex(RuntimeError, "requires login or verification"):
                provider.wait_until_finished(utc_now() + timedelta(minutes=1))

    def test_window_wait_reasserts_title_after_page_render_resets_it(self) -> None:
        provider = DouyinLiveProvider({})
        provider.capture_title = "VideoAgent-Douyin-9229"
        provider._evaluate = MagicMock(return_value=provider.capture_title)  # type: ignore[method-assign]
        provider._visible_chrome_windows = MagicMock(  # type: ignore[method-assign]
            side_effect=[[], [(1234, provider.capture_title)]]
        )
        window = object()

        with patch("pywinauto.Desktop") as desktop, patch(
            "video_agent.providers.douyin_live.time.sleep"
        ), patch(
            "video_agent.providers.douyin_live.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.5, 0.5],
        ):
            desktop.return_value.window.return_value = window
            result = provider._wait_for_chrome_window(10)

        self.assertIs(result, window)
        self.assertIn("document.title", provider._evaluate.call_args.args[0])
        desktop.return_value.window.assert_called_once_with(handle=1234)

    def test_collapses_unique_visible_chatroom(self) -> None:
        provider = DouyinLiveProvider({})
        provider._evaluate = MagicMock(return_value={"status": "collapsed", "before": 320, "after": 0})  # type: ignore[method-assign]

        self.assertTrue(provider._collapse_chatroom(required=True))

        expression = provider._evaluate.call_args.args[0]
        self.assertIn("#chatroom", expression)
        self.assertIn(".chatroom_close", expression)
        self.assertTrue(provider._evaluate.call_args.kwargs["user_gesture"])

    def test_new_chatroom_control_uses_verified_svg_shape_and_location(self) -> None:
        provider = DouyinLiveProvider({})
        provider._evaluate = MagicMock(return_value={"status": "collapsed"})  # type: ignore[method-assign]

        provider._collapse_chatroom(required=True)

        expression = provider._evaluate.call_args.args[0]
        self.assertIn("panel.querySelectorAll('svg')", expression)
        self.assertIn("l-4 4-1.06-1.06", expression)
        self.assertIn("videoAfter > videoBefore + 1", expression)

    def test_does_not_click_when_chatroom_is_already_collapsed(self) -> None:
        provider = DouyinLiveProvider({})
        provider._evaluate = MagicMock(return_value={"status": "already_collapsed"})  # type: ignore[method-assign]

        self.assertFalse(provider._collapse_chatroom(required=True))

    def test_refuses_ambiguous_chatroom_control(self) -> None:
        provider = DouyinLiveProvider({})
        provider._evaluate = MagicMock(return_value={"status": "ambiguous", "count": 2})  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "could not be safely collapsed"):
            provider._collapse_chatroom(required=True)

    def test_chatroom_diagnostic_does_not_repeat_unchanged_status(self) -> None:
        provider = DouyinLiveProvider({})
        provider._evaluate = MagicMock(return_value={"status": "already_collapsed"})  # type: ignore[method-assign]

        provider._collapse_chatroom(required=False)
        provider._collapse_chatroom(required=False)

        self.assertEqual(provider._diagnostics.count("chatroom_already_collapsed"), 1)

    def test_selects_high_definition_and_verifies_visible_label(self) -> None:
        provider = DouyinLiveProvider({})
        provider._evaluate = MagicMock(return_value={"status": "selected"})  # type: ignore[method-assign]

        self.assertTrue(provider._select_high_definition())

        expression = provider._evaluate.call_args.args[0]
        self.assertIn("QualitySwitchNewPlugin", expression)
        self.assertIn("candidates[0].click()", expression)
        self.assertIn("quality_selected_hd", provider._diagnostics)

    def test_rejects_unavailable_high_definition_option(self) -> None:
        provider = DouyinLiveProvider({})
        provider._evaluate = MagicMock(return_value={"status": "option_ambiguous", "count": 0})  # type: ignore[method-assign]

        with self.assertRaisesRegex(RuntimeError, "HD quality could not be selected"):
            provider._select_high_definition()

    def test_waits_for_player_to_fill_viewport_after_sidebar_reflow(self) -> None:
        provider = DouyinLiveProvider({})
        provider._try_start_playback = MagicMock()  # type: ignore[method-assign]
        provider._maintain_capture_title = MagicMock()  # type: ignore[method-assign]
        provider._collapse_chatroom = MagicMock()  # type: ignore[method-assign]
        provider._select_high_definition = MagicMock()  # type: ignore[method-assign]
        provider._evaluate = MagicMock(  # type: ignore[method-assign]
            side_effect=[
                {"ok": True, "playing": True, "x": 0, "width": 1300, "viewportWidth": 1646, "chatroomWidth": 0, "highDefinition": True},
                {"ok": True, "playing": True, "x": 0, "width": 1646, "viewportWidth": 1646, "chatroomWidth": 0, "highDefinition": True},
                {"ok": True, "playing": True, "x": 0, "width": 1646, "viewportWidth": 1646, "chatroomWidth": 0, "highDefinition": True},
            ]
        )

        with patch("video_agent.providers.douyin_live.time.sleep") as sleep:
            provider._verify_player_ready()

        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(0.5)
        provider._collapse_chatroom.assert_called_with(required=False)
        provider._select_high_definition.assert_called_with(required=False)
        self.assertIn("player_ready_after_layout", provider._diagnostics)

    def test_transient_quality_failure_does_not_abort_final_layout_wait(self) -> None:
        provider = DouyinLiveProvider({})
        provider._try_start_playback = MagicMock()  # type: ignore[method-assign]
        provider._maintain_capture_title = MagicMock()  # type: ignore[method-assign]
        provider._collapse_chatroom = MagicMock()  # type: ignore[method-assign]
        provider._select_high_definition = MagicMock(side_effect=[False, True, True])  # type: ignore[method-assign]
        provider._evaluate = MagicMock(  # type: ignore[method-assign]
            side_effect=[
                None,
                {"ok": True, "playing": True, "x": 0, "width": 1646, "viewportWidth": 1646, "chatroomWidth": 0, "highDefinition": True},
                {"ok": True, "playing": True, "x": 0, "width": 1646, "viewportWidth": 1646, "chatroomWidth": 0, "highDefinition": True},
            ]
        )

        with patch("video_agent.providers.douyin_live.time.sleep"):
            provider._verify_player_ready()

        self.assertEqual(provider._select_high_definition.call_count, 3)

    def test_capture_title_installs_mutation_observer_guard(self) -> None:
        provider = DouyinLiveProvider({"debugging_port": 9333})
        provider._evaluate = MagicMock(return_value="VideoAgent-Douyin-9333")  # type: ignore[method-assign]

        provider._set_capture_title()

        expression = provider._evaluate.call_args.args[0]
        self.assertIn("MutationObserver", expression)
        self.assertIn("__videoAgentTitleObserver", expression)
        self.assertEqual(provider.capture_title, "VideoAgent-Douyin-9333")

    def test_capture_health_check_can_be_disabled(self) -> None:
        provider = DouyinLiveProvider({"capture_health_check_seconds": 0})

        self.assertEqual(provider.capture_health_check_seconds(), 0.0)

    def test_shutdown_closes_only_the_task_window(self) -> None:
        window = MagicMock()
        process = MagicMock()
        provider = DouyinLiveProvider({})
        provider.window = window
        provider.process = process

        provider.shutdown_application()

        window.close.assert_called_once()
        process.wait.assert_called_once()

    def test_diagnostic_does_not_write_full_live_url(self) -> None:
        provider = DouyinLiveProvider({})
        provider.live_url = "https://live.douyin.com/64300823902?token=secret"
        with TemporaryDirectory() as directory, patch.object(provider, "_page_state", return_value=None):
            path = provider.capture_diagnostics(Path(directory))
            assert path is not None
            content = path.read_text(encoding="utf-8")
        self.assertIn("url_host=live.douyin.com", content)
        self.assertNotIn("token=secret", content)


if __name__ == "__main__":
    unittest.main()
