from __future__ import annotations

import time
from datetime import timedelta
from typing import Any

from video_agent.config import PlatformConfig
from video_agent.http_client import HttpClient
from video_agent.models import Credentials, FailureReport, MeetingInfo, RecordingTask, TaskStatus, UploadResult, parse_datetime, utc_now
from video_agent.rsa_crypto import decrypt_rsa_credential, ensure_rsa_key_pair, looks_like_rsa_ciphertext


DEFAULT_PROVIDER_ALIASES = {
    "抖音": "douyin_live",
    "抖音直播": "douyin_live",
    "Douyin": "douyin_live",
    "Douyin Live": "douyin_live",
    "腾讯会议": "tencent_meeting",
    "Tencent Meeting": "tencent_meeting",
    "钉钉": "dingtalk",
    "DingTalk": "dingtalk",
    "觅讯": "mixlink",
    "MixLink": "mixlink",
    "mixlink": "mixlink",
    "dingtalk": "dingtalk",
}


class PlatformClient:
    def __init__(self, config: PlatformConfig) -> None:
        self.config = config
        self.http = HttpClient(config.base_url, timeout_seconds=config.timeout_seconds, default_headers=config.headers)
        self._token = config.api_token
        self._token_expires_at = 0.0
        if config.rsa_decrypt_passwords and config.rsa_generate_if_missing:
            ensure_rsa_key_pair(config.rsa_private_key_path, config.rsa_public_key_path)

    def get_next_task(self, agent_id: str) -> RecordingTask | None:
        resp = self.http.get(self.config.pending_tasks_path, headers=self._auth_headers())
        if resp.status_code == 204 or not resp.data:
            return None
        if resp.status_code >= 400:
            raise RuntimeError(f"platform task polling failed: {resp.status_code} {resp.data}")
        tasks = _extract_task_list(resp.data)
        if not tasks:
            return None
        return self._task_from_iectp(tasks[0])

    def report_status(
        self,
        task_id: str,
        status: TaskStatus,
        message: str = "",
        extra: dict[str, Any] | None = None,
        failure: FailureReport | None = None,
    ) -> None:
        extra = extra or {}
        if status == TaskStatus.RECORDING:
            self.report_start(task_id, extra.get("record_start_time"))
            return
        if status == TaskStatus.COMPLETED:
            self.report_complete(task_id, True, extra=extra)
            return
        if status == TaskStatus.FAILED:
            fail_reason = failure.message if failure else message
            self.report_complete(task_id, False, fail_reason=fail_reason, extra=extra)
            return

    def report_start(self, task_id: str, record_start_time: Any | None = None) -> None:
        started = _format_datetime(record_start_time or utc_now())
        body = {"taskId": _task_id_value(task_id), "recordStartTime": started}
        resp = self.http.post(self.config.start_callback_path, body, headers=self._auth_headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"start callback failed: {resp.status_code} {resp.data}")
        _validate_business_response(resp.data, "start callback")

    def report_complete(
        self,
        task_id: str,
        success: bool,
        fail_reason: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        extra = extra or {}
        task = extra.get("task")
        upload = extra.get("upload")
        body: dict[str, Any] = {
            "taskId": _task_id_value(task_id),
            "success": success,
        }
        if fail_reason:
            body["failReason"] = fail_reason
        if isinstance(task, RecordingTask):
            body["videoTitle"] = task.title
            body["trainingTheme"] = str(task.meeting.extra.get("trainingTheme") or "")
        if extra.get("record_start_time"):
            body["recordStartTime"] = _format_datetime(extra["record_start_time"])
        if extra.get("record_end_time"):
            body["recordEndTime"] = _format_datetime(extra["record_end_time"])
        if extra.get("duration") is not None:
            body["duration"] = int(extra["duration"])
        if isinstance(upload, UploadResult):
            body["fileSize"] = upload.file_size
            body["fileChecksum"] = upload.file_checksum
            body["storagePath"] = upload.storage_path
        if extra.get("record_method"):
            body["recordMethod"] = str(extra["record_method"])

        resp = self.http.post(self.config.complete_callback_path, body, headers=self._auth_headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"complete callback failed: {resp.status_code} {resp.data}")
        _validate_business_response(resp.data, "complete callback")

    def _auth_headers(self) -> dict[str, str]:
        token = self._get_token()
        return {"Authorization": token} if token else {}

    def _get_token(self) -> str:
        if self._token and (self.config.api_token or time.time() < self._token_expires_at):
            return self._token
        body = {
            "appId": self.config.app_id,
            "appSecret": self.config.app_secret,
            "tenantId": self.config.tenant_id,
        }
        resp = self.http.post(self.config.token_path, body)
        if resp.status_code >= 400:
            raise RuntimeError(f"token request failed: {resp.status_code} {resp.data}")
        self._token = _extract_token(resp.data)
        self._token_expires_at = time.time() + _extract_expires_in(resp.data) - 30
        return self._token

    def _task_from_iectp(self, data: dict[str, Any]) -> RecordingTask:
        live_platform = str(data.get("livePlatform") or "")
        provider_aliases = {**DEFAULT_PROVIDER_ALIASES, **self.config.provider_aliases}
        provider = provider_aliases.get(live_platform, live_platform or "tencent_meeting")
        meeting_extra = {
            "trainingTheme": data.get("trainingTheme") or "",
            "liveUrl": data.get("liveUrl") or "",
            "accessMethod": data.get("accessMethod") or "",
            "loginMethod": data.get("loginMethod") or "",
            "specialRequirements": data.get("specialRequirements") or "",
            "recordStrategy": data.get("recordStrategy") or "",
            "expectedClarity": data.get("expectedClarity") or "",
            "maxRecordDuration": data.get("maxRecordDuration"),
            "livePlatform": live_platform,
        }
        meeting_password = str(data.get("meetingPassword") or data.get("liveToken") or "")
        return RecordingTask(
            id=str(data["id"]),
            meeting_provider=provider,
            title=str(data.get("videoTitle") or data.get("trainingTheme") or ""),
            start_time=parse_datetime(data["planStartTime"]),
            end_time=_parse_task_end_time(data),
            upload_target="minio",
            credentials=Credentials(
                account=self._decrypt_credential(str(data.get("loginAccount") or "")),
                password=self._decrypt_credential(str(data.get("loginPassword") or "")),
            ),
            meeting=MeetingInfo(
                meeting_no=str(data.get("roomNumber") or data.get("liveUrl") or ""),
                password=self._decrypt_credential(meeting_password),
                extra=meeting_extra,
            ),
            raw=dict(data),
        )

    def _decrypt_credential(self, value: str) -> str:
        if not value or not self.config.rsa_decrypt_passwords:
            return value
        if not looks_like_rsa_ciphertext(value):
            return value
        if self.config.rsa_generate_if_missing:
            ensure_rsa_key_pair(self.config.rsa_private_key_path, self.config.rsa_public_key_path)
        return decrypt_rsa_credential(value, self.config.rsa_private_key_path)


def _extract_task_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data", payload.get("task", payload))
    else:
        data = payload
    if isinstance(data, list):
        return [dict(item) for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [data]
    return []


def _extract_token(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise RuntimeError(f"token response is not an object: {payload}")
    data = payload.get("data")
    candidates: list[Any] = []
    if isinstance(data, dict):
        candidates.extend([data.get("authorization"), data.get("token"), data.get("accessToken")])
    elif isinstance(data, str):
        candidates.append(data)
    candidates.extend([payload.get("authorization"), payload.get("token"), payload.get("accessToken")])
    for candidate in candidates:
        if candidate:
            return str(candidate)
    raise RuntimeError(f"token response missing token: {payload}")


def _extract_expires_in(payload: Any) -> int:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            try:
                return int(data.get("expiresIn") or 3600)
            except (TypeError, ValueError):
                return 3600
    return 3600


def _validate_business_response(payload: Any, label: str) -> None:
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        raise RuntimeError(f"{label} failed: {payload}")


def _parse_task_end_time(data: dict[str, Any]):
    end_value = data.get("planEndTime")
    if not _is_blank_datetime(end_value):
        return parse_datetime(end_value)
    start_time = parse_datetime(data["planStartTime"])
    try:
        duration_minutes = int(data.get("maxRecordDuration") or 60)
    except (TypeError, ValueError):
        duration_minutes = 60
    if duration_minutes <= 0:
        duration_minutes = 60
    return start_time + timedelta(minutes=duration_minutes)


def _is_blank_datetime(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "none", "null"}
    return False


def _task_id_value(task_id: str) -> int | str:
    try:
        return int(task_id)
    except ValueError:
        return task_id


def _format_datetime(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
