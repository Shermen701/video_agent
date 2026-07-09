from __future__ import annotations

from datetime import datetime, timedelta

from video_agent.models import RecordingTask


def should_prepare(task: RecordingTask, now: datetime, prepare_before_minutes: int) -> bool:
    return now >= task.start_time - timedelta(minutes=prepare_before_minutes) and not is_expired(task, now)


def is_expired(task: RecordingTask, now: datetime) -> bool:
    return now >= task.end_time
