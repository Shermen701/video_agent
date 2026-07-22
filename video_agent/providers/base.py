from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any

from video_agent.models import CaptureTarget, Credentials, MeetingInfo


class MeetingProvider(ABC):
    provider_name: str

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config

    @abstractmethod
    def launch(self) -> None:
        """Start the meeting client."""

    @abstractmethod
    def ensure_logged_in(self, credentials: Credentials) -> None:
        """Ensure the meeting client is logged in."""

    @abstractmethod
    def join(self, meeting: MeetingInfo) -> None:
        """Join a meeting."""

    @abstractmethod
    def prepare_audio_video(self) -> None:
        """Disable microphone/camera or apply provider defaults."""

    @abstractmethod
    def wait_until_finished(self, deadline: datetime) -> None:
        """Block until the meeting ends or the fallback deadline is reached."""

    def get_capture_target(self) -> CaptureTarget | None:
        """Return the meeting window to capture, or use the existing OBS scene."""
        return None

    def get_audio_capture_target(self) -> CaptureTarget | None:
        """Return one application whose audio should be captured by OBS."""
        return None

    def capture_health_check_seconds(self) -> float:
        """Return an OBS black-frame check duration, or zero to disable it."""
        return 0.0

    def shutdown_application(self) -> None:
        """Close the provider application after a task, when configured."""
        return None

    @abstractmethod
    def capture_diagnostics(self, task_dir: Path) -> Path | None:
        """Save screenshots/window state for troubleshooting."""

    @abstractmethod
    def cleanup(self) -> None:
        """Clean temporary provider state after task completion."""
