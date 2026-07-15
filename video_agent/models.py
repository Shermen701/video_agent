from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    PREPARING = "preparing"
    JOINING = "joining"
    RECORDING = "recording"
    UPLOADING = "uploading"
    COMPLETED = "completed"
    FAILED = "failed"


class ErrorCode(str, Enum):
    TASK_EXPIRED = "task_expired"
    UNKNOWN_PROVIDER = "unknown_provider"
    OBS_START_FAILED = "obs_start_failed"
    OBS_WEBSOCKET_FAILED = "obs_websocket_failed"
    MEETING_START_FAILED = "meeting_start_failed"
    MEETING_LOGIN_FAILED = "meeting_login_failed"
    MEETING_JOIN_FAILED = "meeting_join_failed"
    RECORDING_FAILED = "recording_failed"
    UPLOAD_FAILED = "upload_failed"
    NO_RECORDING_FILE = "no_recording_file"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class Credentials:
    account: str
    password: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Credentials":
        return cls(account=str(data.get("account", "")), password=str(data.get("password", "")))


@dataclass(frozen=True)
class MeetingInfo:
    meeting_no: str
    password: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MeetingInfo":
        return cls(
            meeting_no=str(data.get("meeting_no", "")),
            password=str(data.get("password", "")),
            extra=dict(data.get("extra") or {}),
        )


@dataclass(frozen=True)
class RecordingTask:
    id: str
    start_time: datetime
    end_time: datetime
    credentials: Credentials
    meeting: MeetingInfo
    meeting_provider: str = "tencent_meeting"
    title: str = ""
    upload_target: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecordingTask":
        return cls(
            id=str(data["id"]),
            meeting_provider=str(data.get("meeting_provider") or "tencent_meeting"),
            title=str(data.get("title") or ""),
            start_time=parse_datetime(data["start_time"]),
            end_time=parse_datetime(data["end_time"]),
            upload_target=str(data.get("upload_target") or ""),
            credentials=Credentials.from_dict(dict(data.get("credentials") or {})),
            meeting=MeetingInfo.from_dict(dict(data.get("meeting") or {})),
            raw=dict(data.get("raw") or {}),
        )


@dataclass(frozen=True)
class FailureReport:
    error_code: ErrorCode | str
    message: str
    log_path: str | None = None
    screenshot_path: str | None = None


@dataclass(frozen=True)
class TaskPaths:
    task_dir: Path
    metadata_path: Path
    log_path: Path
    screenshots_dir: Path


@dataclass(frozen=True)
class UploadResult:
    storage_path: str
    file_size: int
    file_checksum: str
    filename: str


@dataclass(frozen=True)
class CaptureTarget:
    """A native top-level window that OBS can bind to."""

    title: str
    class_name: str
    executable_name: str


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt.astimezone()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).astimezone()
