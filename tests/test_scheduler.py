from __future__ import annotations

import unittest
from datetime import timedelta

from video_agent.models import Credentials, MeetingInfo, RecordingTask, utc_now
from video_agent.task_scheduler import is_expired, should_prepare


def make_task(start_delta_minutes: int, end_delta_minutes: int) -> RecordingTask:
    now = utc_now()
    return RecordingTask(
        id="task-1",
        start_time=now + timedelta(minutes=start_delta_minutes),
        end_time=now + timedelta(minutes=end_delta_minutes),
        credentials=Credentials("account", "password"),
        meeting=MeetingInfo("123"),
    )


class SchedulerTest(unittest.TestCase):
    def test_prepares_inside_five_minute_window(self) -> None:
        task = make_task(start_delta_minutes=4, end_delta_minutes=30)
        self.assertTrue(should_prepare(task, utc_now(), 5))

    def test_does_not_prepare_too_early(self) -> None:
        task = make_task(start_delta_minutes=6, end_delta_minutes=30)
        self.assertFalse(should_prepare(task, utc_now(), 5))

    def test_expired_task_is_not_ready(self) -> None:
        task = make_task(start_delta_minutes=-20, end_delta_minutes=-1)
        self.assertTrue(is_expired(task, utc_now()))
        self.assertFalse(should_prepare(task, utc_now(), 5))


if __name__ == "__main__":
    unittest.main()
