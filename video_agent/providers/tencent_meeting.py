from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from video_agent.app_discovery import find_tencent_meeting_executable
from video_agent.models import Credentials, MeetingInfo
from video_agent.providers.base import MeetingProvider


class TencentMeetingProvider(MeetingProvider):
    provider_name = "tencent_meeting"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.app = None
        self.window = None
        self.window_title_regex = str(config.get("window_title_regex") or "腾讯会议|Tencent Meeting")

    def launch(self) -> None:
        try:
            self._connect_window(timeout_seconds=3)
            return
        except Exception:
            pass
        executable = find_tencent_meeting_executable(str(self.config.get("executable_path") or ""))
        if executable is None:
            raise RuntimeError("Tencent Meeting executable not found")
        subprocess.Popen([str(executable)], cwd=str(executable.parent))
        self._connect_window(timeout_seconds=20)

    def ensure_logged_in(self, credentials: Credentials) -> None:
        self._ensure_pywinauto()
        if self.config.get("assume_logged_in") and not self._is_login_page():
            return
        # If the main window is already present and no obvious login entry exists, treat it as logged in.
        login_retry_count = int(self.config.get("login_retry_count") or 2)
        for _ in range(login_retry_count):
            self._connect_window(timeout_seconds=5)
            if self._is_login_page():
                self._dismiss_agreement_prompt_if_present()
                self._accept_agreement_if_present()
                self._click_phone_login_if_present()
                time.sleep(1)
                self._dismiss_agreement_prompt_if_present()
                self._fill_login_credentials(credentials)
                self._accept_agreement_if_present()
                self._click_login()
                time.sleep(3)
                continue
            return
        if self._is_login_page():
            raise RuntimeError("Tencent Meeting login failed")

    def join(self, meeting: MeetingInfo) -> None:
        self._ensure_pywinauto()
        join_retry_count = int(self.config.get("join_retry_count") or 2)
        for _ in range(join_retry_count):
            self._connect_window(timeout_seconds=5)
            if not self._click_if_present(["加入会议", "Join"]):
                self._click_home_join_fallback()
            time.sleep(1)
            if self._is_login_page():
                raise RuntimeError("Tencent Meeting is still on login page")
            if self._has_text(["为保障您的合法权益"]):
                raise RuntimeError("Tencent Meeting agreement dialog is blocking login")
            if not self._edit_controls() and self.config.get("assume_joined_when_join_form_missing"):
                return
            self._fill_first_edit(meeting.meeting_no)
            if meeting.password:
                self._fill_next_edit(meeting.password)
            self._click_first(["加入会议", "入会", "Join"])
            time.sleep(5)
            if not self._has_text(["加入会议"]):
                return
        raise RuntimeError("Tencent Meeting join failed")

    def prepare_audio_video(self) -> None:
        self._ensure_pywinauto()
        self._click_if_present(["静音", "解除静音", "麦克风"])
        self._click_if_present(["关闭视频", "开启视频", "摄像头"])

    def wait_until_finished(self, deadline: datetime) -> None:
        poll_seconds = int(self.config.get("meeting_end_poll_seconds") or 5)
        while datetime.now(deadline.tzinfo).astimezone() < deadline:
            if self._meeting_has_finished():
                return
            time.sleep(poll_seconds)

    def capture_diagnostics(self, task_dir: Path) -> Path | None:
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / "diagnostic.txt"
        lines = ["Tencent Meeting diagnostics"]
        try:
            self._connect_window(timeout_seconds=1)
            if self.window is not None:
                lines.append(str(self.window.window_text()))
                lines.append(str(self.window.texts()))
        except Exception as exc:
            lines.append(f"diagnostic_error={exc}")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def cleanup(self) -> None:
        self.window = None

    def _meeting_has_finished(self) -> bool:
        if self.config.get("disable_meeting_end_text_detection"):
            return False
        try:
            self._connect_window(timeout_seconds=1)
        except Exception:
            return True
        if self._is_login_page():
            return False
        return self._has_text(["加入会议", "快速会议", "预定会议", "会议已结束"])

    def _connect_window(self, timeout_seconds: int) -> None:
        self._ensure_pywinauto()
        from pywinauto import Application, Desktop  # type: ignore

        pattern = re.compile(self.window_title_regex)
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                handle = self._find_window_handle(pattern)
                if handle:
                    self.window = Desktop(backend="uia").window(handle=handle)
                    try:
                        self.app = Application(backend="uia").connect(handle=handle)
                    except Exception:
                        self.app = None
                    self._bring_window_to_front()
                    return
            except Exception as exc:
                last_error = exc
            time.sleep(1)
        raise RuntimeError("Tencent Meeting window not found") from last_error

    def _has_text(self, texts: list[str]) -> bool:
        if self.window is None:
            return False
        current = "\n".join(self._control_texts())
        return any(text in current for text in texts)

    def _control_texts(self) -> list[str]:
        if self.window is None:
            return []
        values: list[str] = []
        try:
            values.extend(str(item) for item in self.window.texts())
        except Exception:
            pass
        try:
            for control in self.window.descendants():
                text = str(control.window_text() or "")
                if text:
                    values.append(text)
        except Exception:
            pass
        return values

    def _is_login_page(self) -> bool:
        return self._has_text(["我已阅读并同意", "手机号", "邮箱", "SSO", "企业微信", "微信登录"])

    def _click_phone_login_if_present(self) -> None:
        if self._click_if_present(["手机号"]):
            return
        self._click_ratio(self.config.get("phone_login_click_ratio") or [0.17, 0.76])

    def _accept_agreement_if_present(self) -> None:
        if not self._has_text(["我已阅读并同意"]):
            return
        if self._click_if_present(["我已阅读并同意"]):
            return
        rect = self._text_rectangle("我已阅读并同意")
        if rect is not None:
            self._click_absolute(rect.left - 18, rect.top + rect.height() // 2)
            return
        self._click_ratio(self.config.get("agreement_click_ratio") or [0.17, 0.92])

    def _dismiss_agreement_prompt_if_present(self) -> None:
        if self._click_if_present(["同意"]):
            time.sleep(0.5)
            return
        rect = self._text_rectangle("为保障您的合法权益")
        if rect is None:
            return
        self._click_absolute(rect.left + rect.width() - 35, rect.bottom + 55)
        time.sleep(0.5)

    def _click_first(self, names: list[str]) -> None:
        if not self._click_if_present(names):
            raise RuntimeError(f"control not found: {names}")

    def _click_login(self) -> None:
        if self._click_if_present(["登录", "Login"]):
            return
        self._click_ratio(self.config.get("login_click_ratio") or [0.5, 0.53])

    def _click_if_present(self, names: list[str]) -> bool:
        if self.window is None:
            return False
        for name in names:
            try:
                self.window.child_window(title_re=f".*{re.escape(name)}.*").click_input()
                return True
            except Exception:
                continue
        return False

    def _click_home_join_fallback(self) -> None:
        if self.window is None:
            raise RuntimeError("Tencent Meeting window is not connected")

        self._bring_window_to_front()
        ratio = self.config.get("home_join_click_ratio") or [0.354, 0.341]
        if not isinstance(ratio, list | tuple) or len(ratio) != 2:
            raise RuntimeError("home_join_click_ratio must be a two-item list")
        left, top, width, height = self._window_bounds()
        x = int(left + width * float(ratio[0]))
        y = int(top + height * float(ratio[1]))
        self._native_click(x, y)

    def _click_ratio(self, ratio: Any) -> None:
        if self.window is None:
            raise RuntimeError("Tencent Meeting window is not connected")

        self._bring_window_to_front()
        if not isinstance(ratio, list | tuple) or len(ratio) != 2:
            raise RuntimeError("click ratio must be a two-item list")
        left, top, width, height = self._window_bounds()
        x = int(left + width * float(ratio[0]))
        y = int(top + height * float(ratio[1]))
        self._native_click(x, y)

    def _click_absolute(self, x: int, y: int) -> None:
        self._bring_window_to_front()
        self._native_click(int(x), int(y))

    def _text_rectangle(self, needle: str) -> Any | None:
        if self.window is None:
            return None
        try:
            for control in self.window.descendants():
                if needle in str(control.window_text() or ""):
                    return control.rectangle()
        except Exception:
            return None
        return None

    def _window_bounds(self) -> tuple[int, int, int, int]:
        if self.window is None:
            raise RuntimeError("Tencent Meeting window is not connected")
        handle = int(self.window.handle)
        try:
            import win32gui  # type: ignore

            left, top, right, bottom = win32gui.GetWindowRect(handle)
            return left, top, right - left, bottom - top
        except Exception:
            rect = self.window.rectangle()
            return rect.left, rect.top, rect.width(), rect.height()

    @staticmethod
    def _native_click(x: int, y: int) -> None:
        try:
            import win32api  # type: ignore
            import win32con  # type: ignore

            win32api.SetCursorPos((x, y))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
            return
        except Exception:
            pass
        import ctypes

        user32 = ctypes.windll.user32
        user32.SetCursorPos(int(x), int(y))
        user32.mouse_event(0x0002, 0, 0, 0, 0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)

    @staticmethod
    def _find_window_handle(pattern: re.Pattern[str]) -> int | None:
        try:
            import win32gui  # type: ignore
            import win32process  # type: ignore

            handles: list[int] = []

            def callback(hwnd: int, _lparam: object) -> bool:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd)
                try:
                    _thread_id, pid = win32process.GetWindowThreadProcessId(hwnd)
                except Exception:
                    pid = 0
                if pattern.search(title or ""):
                    handles.append(hwnd)
                    return False
                if title == "腾讯会议":
                    handles.append(hwnd)
                    return False
                return True

            win32gui.EnumWindows(callback, None)
            return handles[0] if handles else None
        except Exception:
            return None

    def _bring_window_to_front(self) -> None:
        if self.window is None:
            return
        try:
            import win32con  # type: ignore
            import win32gui  # type: ignore

            win32gui.ShowWindow(self.window.handle, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(self.window.handle)
            return
        except Exception:
            pass
        try:
            self.window.set_focus()
        except Exception:
            return

    def _fill_first_edit(self, text: str) -> None:
        edits = self._edit_controls()
        if not edits:
            raise RuntimeError("input control not found")
        self._set_edit_text(edits[0], text)

    def _fill_next_edit(self, text: str) -> None:
        edits = self._edit_controls()
        if len(edits) < 2:
            return
        self._set_edit_text(edits[1], text)

    def _fill_login_credentials(self, credentials: Credentials) -> None:
        edits = self._edit_controls()
        if len(edits) >= 3:
            self._set_edit_text(edits[1], credentials.account)
            self._set_edit_text(edits[2], credentials.password)
            return
        if len(edits) >= 2:
            self._set_edit_text(edits[0], credentials.account)
            self._set_edit_text(edits[1], credentials.password)
            return
        raise RuntimeError("login input controls not found")

    @staticmethod
    def _set_edit_text(edit: Any, text: str) -> None:
        try:
            edit.set_edit_text("")
            edit.set_edit_text(text)
            return
        except Exception:
            pass
        edit.click_input()
        edit.type_keys("^a{BACKSPACE}", set_foreground=False)
        edit.type_keys(text, with_spaces=True, set_foreground=False)

    def _edit_controls(self) -> list[Any]:
        if self.window is None:
            return []
        return list(self.window.descendants(control_type="Edit"))

    @staticmethod
    def _ensure_pywinauto() -> None:
        try:
            import pywinauto  # noqa: F401
        except ModuleNotFoundError as exc:
            raise RuntimeError("pywinauto is not installed; run pip install -r requirements.txt") from exc
