from __future__ import annotations

import unittest
from pathlib import Path

from video_agent.config import PlatformConfig
from video_agent.http_client import HttpResponse
from video_agent.models import TaskStatus, UploadResult
from video_agent.platform_client import PlatformClient
from video_agent.rsa_crypto import encrypt_rsa_credential, ensure_rsa_key_pair


class FakeHttp:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict, dict | None]] = []
        self.gets: list[tuple[str, dict | None]] = []

    def post(self, path: str, body: dict, headers: dict[str, str] | None = None) -> HttpResponse:
        self.posts.append((path, body, headers))
        if path == "/api/iectp/api/open/token":
            return HttpResponse(200, {"code": 0, "data": {"authorization": "token-1", "expiresIn": 3600}})
        return HttpResponse(200, {"code": 0, "data": None})

    def get(self, path: str, headers: dict[str, str] | None = None) -> HttpResponse:
        self.gets.append((path, headers))
        return HttpResponse(
            200,
            {
                "code": 0,
                "data": [
                    {
                        "id": 123,
                        "trainingTheme": "安全培训",
                        "videoTitle": "第一课",
                        "livePlatform": "腾讯会议",
                        "accessMethod": "room",
                        "liveUrl": "https://meeting.example",
                        "loginMethod": "account",
                        "loginAccount": "user",
                        "loginPassword": "pass",
                        "roomNumber": "999",
                        "liveToken": "0000",
                        "specialRequirements": "none",
                        "planStartTime": "2026-06-25 10:00:00",
                        "planEndTime": "2026-06-25 11:00:00",
                        "maxRecordDuration": 60,
                        "recordStrategy": "full",
                        "expectedClarity": "1080p",
                    }
                ],
            },
        )


class PlatformClientTest(unittest.TestCase):
    def test_get_next_task_fetches_token_and_maps_iectp_task(self) -> None:
        client = PlatformClient(
            PlatformConfig(
                app_id="app",
                app_secret="secret",
                tenant_id=1,
                headers={"x-userid": "428724", "x-tenantId": "1", "x-accountno": "241024010"},
                provider_aliases={"腾讯会议": "tencent_meeting"},
                rsa_generate_if_missing=False,
            )
        )
        fake_http = FakeHttp()
        client.http = fake_http  # type: ignore[assignment]

        task = client.get_next_task("recorder-001")

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.id, "123")
        self.assertEqual(task.meeting_provider, "tencent_meeting")
        self.assertEqual(task.title, "第一课")
        self.assertEqual(task.credentials.account, "user")
        self.assertEqual(task.meeting.meeting_no, "999")
        self.assertEqual(task.meeting.password, "0000")
        self.assertEqual(task.meeting.extra["trainingTheme"], "安全培训")
        self.assertEqual(fake_http.posts[0][1], {"appId": "app", "appSecret": "secret", "tenantId": 1})
        self.assertEqual(fake_http.gets[0], ("/api/iectp/externalRecord/pendingTasks", {"Authorization": "token-1"}))

    def test_callbacks_use_authorization_header_without_bearer(self) -> None:
        client = PlatformClient(PlatformConfig(api_token="token-1", rsa_generate_if_missing=False))
        fake_http = FakeHttp()
        client.http = fake_http  # type: ignore[assignment]

        client.report_status("123", TaskStatus.RECORDING, extra={"record_start_time": "2026-06-25 10:00:00"})
        client.report_status(
            "123",
            TaskStatus.COMPLETED,
            extra={
                "upload": UploadResult(
                    storage_path="/external-record/123/a.mp4",
                    file_size=10,
                    file_checksum="f" * 64,
                    filename="a.mp4",
                ),
                "record_start_time": "2026-06-25 10:00:00",
                "record_end_time": "2026-06-25 10:30:00",
                "duration": 1800,
                "record_method": "OBS",
            },
        )

        self.assertEqual(fake_http.posts[0][0], "/api/iectp/externalRecord/startCallback")
        self.assertEqual(fake_http.posts[0][2], {"Authorization": "token-1"})
        self.assertEqual(fake_http.posts[0][1], {"taskId": 123, "recordStartTime": "2026-06-25 10:00:00"})
        self.assertEqual(fake_http.posts[1][0], "/api/iectp/externalRecord/completeCallback")
        self.assertEqual(fake_http.posts[1][2], {"Authorization": "token-1"})
        self.assertEqual(fake_http.posts[1][1]["success"], True)
        self.assertEqual(fake_http.posts[1][1]["storagePath"], "/external-record/123/a.mp4")

    def test_decrypts_rsa_encrypted_login_credentials(self) -> None:
        key_dir = Path("test_outputs") / "rsa"
        private_key = key_dir / "private.pem"
        public_key = key_dir / "public.pem"
        ensure_rsa_key_pair(private_key, public_key)
        encrypted_account = encrypt_rsa_credential("13117414114", public_key)
        encrypted_password = encrypt_rsa_credential("meeting-secret", public_key)
        fake_http = FakeHttp()
        original_get = fake_http.get

        def get_with_encrypted_credentials(path: str, headers: dict[str, str] | None = None) -> HttpResponse:
            response = original_get(path, headers)
            response.data["data"][0]["loginAccount"] = encrypted_account
            response.data["data"][0]["loginPassword"] = encrypted_password
            return response

        fake_http.get = get_with_encrypted_credentials  # type: ignore[method-assign]
        client = PlatformClient(
            PlatformConfig(
                api_token="token-1",
                rsa_private_key_path=str(private_key),
                rsa_public_key_path=str(public_key),
                rsa_generate_if_missing=False,
            )
        )
        client.http = fake_http  # type: ignore[assignment]

        task = client.get_next_task("recorder-001")

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.credentials.account, "13117414114")
        self.assertEqual(task.credentials.password, "meeting-secret")

    def test_maps_dingtalk_task_provider_and_credentials(self) -> None:
        fake_http = FakeHttp()
        original_get = fake_http.get

        def get_with_dingtalk(path: str, headers: dict[str, str] | None = None) -> HttpResponse:
            response = original_get(path, headers)
            response.data["data"][0]["livePlatform"] = "钉钉"
            response.data["data"][0]["loginAccount"] = "13800138000"
            response.data["data"][0]["loginPassword"] = "ding-pass"
            response.data["data"][0]["roomNumber"] = "123456789"
            response.data["data"][0]["liveToken"] = "8888"
            return response

        fake_http.get = get_with_dingtalk  # type: ignore[method-assign]
        client = PlatformClient(
            PlatformConfig(
                api_token="token-1",
                provider_aliases={"钉钉": "dingtalk"},
                rsa_generate_if_missing=False,
            )
        )
        client.http = fake_http  # type: ignore[assignment]

        task = client.get_next_task("recorder-001")

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.meeting_provider, "dingtalk")
        self.assertEqual(task.credentials.account, "13800138000")
        self.assertEqual(task.credentials.password, "ding-pass")
        self.assertEqual(task.meeting.meeting_no, "123456789")
        self.assertEqual(task.meeting.password, "8888")

    def test_maps_dingtalk_with_default_provider_aliases(self) -> None:
        fake_http = FakeHttp()
        original_get = fake_http.get

        def get_with_dingtalk(path: str, headers: dict[str, str] | None = None) -> HttpResponse:
            response = original_get(path, headers)
            response.data["data"][0]["livePlatform"] = "钉钉"
            return response

        fake_http.get = get_with_dingtalk  # type: ignore[method-assign]
        client = PlatformClient(PlatformConfig(api_token="token-1", rsa_generate_if_missing=False))
        client.http = fake_http  # type: ignore[assignment]

        task = client.get_next_task("recorder-001")

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.meeting_provider, "dingtalk")

    def test_maps_blank_plan_end_time_from_max_record_duration(self) -> None:
        fake_http = FakeHttp()
        original_get = fake_http.get

        def get_with_blank_plan_end_time(path: str, headers: dict[str, str] | None = None) -> HttpResponse:
            response = original_get(path, headers)
            response.data["data"][0]["planEndTime"] = "None"
            response.data["data"][0]["maxRecordDuration"] = 45
            return response

        fake_http.get = get_with_blank_plan_end_time  # type: ignore[method-assign]
        client = PlatformClient(PlatformConfig(api_token="token-1", rsa_generate_if_missing=False))
        client.http = fake_http  # type: ignore[assignment]

        task = client.get_next_task("recorder-001")

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.start_time.minute, 0)
        self.assertEqual(task.end_time.hour, 10)
        self.assertEqual(task.end_time.minute, 45)


if __name__ == "__main__":
    unittest.main()
