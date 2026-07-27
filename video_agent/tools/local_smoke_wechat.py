"""Exercise WeChat Channels navigation and optional local OBS recording.

Run this command from the unlocked Windows desktop session that owns WeChat.
The optional recording mode writes only a local file and never contacts the task
platform or uploads anything. It never sends messages or closes WeChat.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

from video_agent.config import load_config
from video_agent.models import Credentials
from video_agent.obs_controller import ObsController
from video_agent.providers.wechat_live import WeChatLiveProvider
from video_agent.tools.inspect_wechat import main as inspect_wechat


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--search-command", default="央视网")
    parser.add_argument("--output-dir", default="test_outputs/wechat_smoke")
    parser.add_argument(
        "--enter-live",
        action="store_true",
        help="Open the detected live card, but do not start OBS.",
    )
    parser.add_argument(
        "--record-seconds",
        type=int,
        default=0,
        help="After entering the live room, record locally with OBS for this many seconds; never uploads.",
    )
    args = parser.parse_args(argv)
    if args.record_seconds < 0:
        parser.error("--record-seconds cannot be negative")
    if args.record_seconds and not args.enter_live:
        parser.error("--record-seconds requires --enter-live")

    config = load_config(args.config)
    provider = WeChatLiveProvider(dict(config.providers.get("wechat_live") or {}))
    provider.search_command = str(args.search_command)
    output_dir = Path(args.output_dir)
    obs: ObsController | None = None
    recording_started = False
    capture_configured = False
    try:
        provider.launch()
        provider.ensure_logged_in(Credentials("", ""))
        provider._navigate_to_followed_channel(args.search_command)
        if args.enter_live:
            live_timeout = max(
                provider._navigation_timeout(),
                float(provider.config.get("page_transition_timeout_seconds") or 30),
            )
            deadline = time.monotonic() + live_timeout
            while True:
                video_window = provider._require_video_window()
                live_card = provider._find_exact_control(video_window, ["直播中"])
                if live_card is not None:
                    provider._click_control(live_card)
                    if provider._wait_for_any_text(
                        provider._require_video_window(),
                        ("的直播", "评论已关闭", "聊一聊"),
                        timeout_seconds=8,
                    ):
                        break
                    provider._diagnostics.append("smoke_live_card_click_retrying")
                if time.monotonic() >= deadline:
                    raise RuntimeError("meeting_join_failed: live room did not finish loading")
                provider._refresh_channel_homepage()
                time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
            provider.live_window = provider._find_live_window(provider._navigation_timeout())
            if provider.live_window is None:
                raise RuntimeError("meeting_join_failed: WeChat live window was not found")
            provider._diagnostics.append("smoke_live_room_opened")
        if args.record_seconds:
            obs = ObsController(config.obs)
            obs.ensure_running()
            obs.connect()
            target = provider.get_capture_target()
            if target is None:
                raise RuntimeError("recording_failed: WeChat live capture target is unavailable")
            obs.configure_window_capture(target)
            capture_configured = True
            audio_target = provider.get_audio_capture_target()
            if audio_target is not None:
                obs.configure_application_audio_capture(audio_target)
            recording_dir = output_dir / "recording"
            obs.start_recording(recording_dir)
            recording_started = True
            print(f"OBS local recording started for {args.record_seconds} seconds", flush=True)
            if not obs.verify_window_capture_visible(
                output_dir / "obs-capture-black.png", duration_seconds=5
            ):
                raise RuntimeError("recording_failed: OBS window capture remained black")
            time.sleep(args.record_seconds)
            obs.stop_recording()
            recording_started = False
            obs.restore_capture_scene()
            capture_configured = False
            recording = obs.find_latest_recording(recording_dir)
            if recording is None:
                raise RuntimeError("recording_failed: OBS did not write a local recording")
            print(f"local recording={recording.resolve()}", flush=True)
        provider.capture_diagnostics(output_dir)
        inspect_wechat(["--output-dir", str(output_dir / "inspect")])
        print(f"opened followed Channels account: {args.search_command}")
    except Exception:
        provider.capture_diagnostics(output_dir)
        raise
    finally:
        if obs is not None and recording_started:
            try:
                obs.stop_recording()
            except Exception:
                pass
        if obs is not None and capture_configured:
            try:
                obs.restore_capture_scene()
            except Exception:
                pass
        provider.cleanup()


if __name__ == "__main__":
    main()
