from __future__ import annotations

import unittest
from pathlib import Path

from video_agent.app_discovery import find_dingtalk_executable


class AppDiscoveryTest(unittest.TestCase):
    def test_dingtalk_override_path_is_used_when_it_exists(self) -> None:
        output_dir = Path("test_outputs") / "app_discovery"
        output_dir.mkdir(parents=True, exist_ok=True)
        executable = output_dir / "DingtalkLauncher.exe"
        executable.write_text("", encoding="utf-8")

        self.assertEqual(find_dingtalk_executable(str(executable)), executable)

    def test_dingtalk_override_path_returns_none_when_missing(self) -> None:
        missing = Path("test_outputs") / "app_discovery" / "missing.exe"

        self.assertIsNone(find_dingtalk_executable(str(missing)))


if __name__ == "__main__":
    unittest.main()
