from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from video_agent.process_control import (
    ProcessInfo,
    shutdown_matching_processes,
    wait_for_matching_processes_exit,
)


class ProcessControlTest(unittest.TestCase):
    def test_wait_for_matching_processes_exit_ignores_unrelated_same_name(self) -> None:
        expected = Path(r"D:\OBS\obs64.exe")
        unrelated = ProcessInfo(21, "obs64.exe", Path(r"C:\Other\obs64.exe"))
        with patch("video_agent.process_control._list_processes", return_value=[unrelated]):
            self.assertTrue(
                wait_for_matching_processes_exit(
                    executable_names={"obs64.exe"}, exact_paths={expected}, timeout_seconds=0
                )
            )
    def test_only_closes_matching_names_inside_allowed_root(self) -> None:
        processes = [
            ProcessInfo(10, "DingTalk.exe", Path(r"D:\DingDing\main\DingTalk.exe")),
            ProcessInfo(11, "tblive.exe", Path(r"D:\DingDing\plugins\tblive.exe")),
            ProcessInfo(12, "DingTalk.exe", Path(r"C:\Unrelated\DingTalk.exe")),
            ProcessInfo(13, "other.exe", Path(r"D:\DingDing\other.exe")),
        ]
        closed: list[set[int]] = []
        terminated: list[int] = []

        with patch("video_agent.process_control._list_processes", return_value=processes), patch(
            "video_agent.process_control._post_close_to_windows",
            side_effect=lambda pids: closed.append(pids),
        ), patch(
            "video_agent.process_control._wait_for_exit", return_value=[11]
        ), patch(
            "video_agent.process_control._terminate_process",
            side_effect=lambda pid: terminated.append(pid),
        ):
            result = shutdown_matching_processes(
                executable_names={"DingTalk.exe", "tblive.exe"},
                allowed_roots={Path(r"D:\DingDing")},
            )

        self.assertEqual(result, [10, 11])
        self.assertEqual(closed, [{10, 11}])
        self.assertEqual(terminated, [11])

    def test_exact_path_rejects_same_named_obs_elsewhere(self) -> None:
        expected = Path(r"D:\OBS\obs64.exe")
        processes = [
            ProcessInfo(20, "obs64.exe", expected),
            ProcessInfo(21, "obs64.exe", Path(r"C:\Other\obs64.exe")),
        ]

        with patch("video_agent.process_control._list_processes", return_value=processes), patch(
            "video_agent.process_control._post_close_to_windows"
        ) as close, patch("video_agent.process_control._wait_for_exit", return_value=[]):
            result = shutdown_matching_processes(
                executable_names={"obs64.exe"}, exact_paths={expected}
            )

        self.assertEqual(result, [20])
        close.assert_called_once_with({20})


if __name__ == "__main__":
    unittest.main()
