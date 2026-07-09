from __future__ import annotations

from video_agent.app_discovery import find_dingtalk_executable, find_obs_executable, find_tencent_meeting_executable


def main() -> None:
    obs = find_obs_executable()
    meeting = find_tencent_meeting_executable()
    dingtalk = find_dingtalk_executable()
    print(f"OBS: {obs if obs else 'not found'}")
    print(f"Tencent Meeting: {meeting if meeting else 'not found'}")
    print(f"DingTalk: {dingtalk if dingtalk else 'not found'}")


if __name__ == "__main__":
    main()
