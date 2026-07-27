from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from video_agent.app_discovery import find_tencent_meeting_executable
from video_agent.models import CaptureTarget, Credentials, MeetingInfo
from video_agent.process_control import shutdown_matching_processes
from video_agent.providers.base import MeetingProvider


class TencentMeetingProvider(MeetingProvider):
    provider_name = "tencent_meeting"
    LOGIN_GUIDE_PAGE = "QApplication.wemeet://page/guide"
    IN_MEETING_PAGE = "QApplication.wemeet://page/inmeeting"
    AFTER_MEETING_CONFIRM_AUTOMATION_ID = (
        "QApplication.AfterMeetingDialog.DialogWidget.MainWidget."
        "conclusion_widget.meeting_conclusion_content.QFWidget.conform_button"
    )

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.app = None
        self.window = None
        self.meeting_window = None
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
        # If the main window is already present and no obvious login entry exists, treat it as logged in.
        login_retry_count = int(self.config.get("login_retry_count") or 2)
        for _ in range(login_retry_count):
            self._connect_window(timeout_seconds=5)
            if self._is_login_page():
                if len(self._edit_controls()) < 2:
                    self._wait_for_login_guide_ready()
                    self._click_phone_login_if_present()
                    self._wait_for_phone_login_form()
                self._select_password_login()
                self._wait_for_login_edits()
                self._fill_login_credentials(credentials)
                time.sleep(float(self.config.get("login_input_settle_seconds") or 1.0))
                self._click_login()
                if self._wait_until_login_complete():
                    return
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
            if not self._connect_join_window(timeout_seconds=5):
                if self._connect_in_meeting_window(timeout_seconds=1):
                    return
                continue
            if self._is_login_page():
                raise RuntimeError("Tencent Meeting is still on login page")
            if self._has_text(["为保障您的合法权益"]):
                raise RuntimeError("Tencent Meeting agreement dialog is blocking login")
            self._fill_first_edit(meeting.meeting_no)
            self._click_first(["加入会议", "入会", "Join"])
            if self._wait_for_meeting_after_submit(meeting):
                return
        raise RuntimeError("Tencent Meeting join failed")

    def prepare_audio_video(self) -> None:
        self._ensure_pywinauto()
        self._connect_in_meeting_window(timeout_seconds=5)
        self._select_computer_audio_if_present()
        self._click_if_present(["静音", "解除静音", "麦克风"])
        self._click_if_present(["关闭视频", "开启视频", "摄像头"])

    def get_capture_target(self) -> CaptureTarget | None:
        """Bind OBS to this task's Tencent Meeting window, never a stale source."""
        if self.meeting_window is None and not self._connect_in_meeting_window(timeout_seconds=2):
            raise RuntimeError("recording_failed: Tencent Meeting window is unavailable")
        if self.meeting_window is None:
            raise RuntimeError("recording_failed: Tencent Meeting window is unavailable")
        try:
            handle = int(self.meeting_window.handle)
        except Exception as exc:
            raise RuntimeError("recording_failed: Tencent Meeting window handle is unavailable") from exc
        if not self._window_is_available(handle):
            raise RuntimeError("recording_failed: Tencent Meeting window is no longer visible")
        return self._capture_target_from_handle(handle)

    def capture_health_check_seconds(self) -> float:
        """Reject a black OBS window capture before a bad recording is uploaded."""
        return max(0.0, float(self.config.get("capture_health_check_seconds", 5)))

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
        self.meeting_window = None
        self.window = None

    def shutdown_application(self) -> None:
        self._logout_current_account()
        executable = find_tencent_meeting_executable(
            str(self.config.get("executable_path") or "")
        )
        if executable is None:
            return
        shutdown_matching_processes(
            executable_names={"WeMeetApp.exe"},
            allowed_roots={executable.parent},
            timeout_seconds=float(self.config.get("shutdown_timeout_seconds") or 5),
        )

    def _logout_current_account(self) -> None:
        try:
            self._connect_window(timeout_seconds=3)
        except RuntimeError:
            return
        if self._is_login_page():
            return

        from pywinauto import Desktop, mouse  # type: ignore

        self._open_home_settings(mouse)

        deadline = time.monotonic() + 5
        settings = None
        while time.monotonic() < deadline:
            for window in Desktop(backend="uia").windows():
                if str(window.element_info.automation_id or "") == "QApplication.SettingPanelDialog":
                    settings = window
                    break
            if settings is not None:
                break
            time.sleep(0.2)
        if settings is None:
            raise RuntimeError("Tencent Meeting settings window did not open")

        account_security = next(
            (
                control
                for control in settings.descendants()
                if str(control.window_text() or "") == "账号安全与隐私"
            ),
            None,
        )
        if account_security is None:
            raise RuntimeError("Tencent Meeting account security settings not found")
        account_security.click_input()
        time.sleep(0.5)

        rect = settings.rectangle()
        scroll_point = (
            int(rect.left + rect.width() * 0.75),
            int(rect.top + rect.height() * 0.75),
        )
        mouse.move(coords=scroll_point)
        mouse.scroll(coords=scroll_point, wheel_dist=-20)
        time.sleep(0.5)
        self._click_logout_control(settings)

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self._connect_window(timeout_seconds=1)
            except RuntimeError:
                continue
            if self._is_login_page():
                return
        raise RuntimeError("Tencent Meeting logout did not return to the login page")

    def _open_home_settings(self, mouse: Any | None = None) -> None:
        settings_entry = self._find_home_settings_entry()
        if settings_entry is not None:
            rect = settings_entry.rectangle()
            self._click_absolute(rect.left + rect.width() // 2, rect.top + rect.height() // 2)
            return
        if mouse is None:
            from pywinauto import mouse  # type: ignore
        settings_ratio = self.config.get("home_settings_click_ratio") or [0.063, 0.831]
        left, top, width, height = self._window_bounds()
        mouse.click(
            coords=(
                int(left + width * float(settings_ratio[0])),
                int(top + height * float(settings_ratio[1])),
            )
        )

    def _find_home_settings_entry(self) -> Any | None:
        """Find the settings icon as the middle item in the sidebar's bottom trio."""
        if self.window is None:
            return None
        try:
            window_rect = self.window.rectangle()
            controls = self.window.descendants()
        except Exception:
            return None
        sidebar_icons: list[Any] = []
        for control in controls:
            try:
                automation_id = str(control.element_info.automation_id or "")
                if ".NXQtImage:" not in automation_id:
                    continue
                rect = control.rectangle()
                if not (40 <= rect.width() <= 60 and 40 <= rect.height() <= 60):
                    continue
                if rect.left > window_rect.left + 160:
                    continue
                sidebar_icons.append(control)
            except Exception:
                continue
        if len(sidebar_icons) < 3:
            return None
        sidebar_icons.sort(key=lambda control: control.rectangle().top)
        return sidebar_icons[-2]

    def _click_logout_control(self, settings: Any) -> None:
        """Click the bottom-most visible action in the account-security page.

        Tencent Meeting exposes the label inside the logout button as an unnamed
        ``NXQtText`` node.  Its automation id changes each run, so select it by
        its stable containment, right-column placement, and bottom-most order
        after the account-security page has been scrolled to the end.
        """
        logout = self._find_logout_control(settings)
        if logout is None:
            raise RuntimeError("Tencent Meeting logout control not found")
        rect = logout.rectangle()
        self._click_absolute(rect.left + rect.width() // 2, rect.top + rect.height() // 2)

    @staticmethod
    def _find_logout_control(settings: Any) -> Any | None:
        try:
            settings_rect = settings.rectangle()
            controls = settings.descendants()
        except Exception:
            return None

        candidates: list[Any] = []
        content_left = settings_rect.left + settings_rect.width() * 0.75
        for control in controls:
            try:
                automation_id = str(control.element_info.automation_id or "")
                if "SettingPageContainer" not in automation_id or ".NXQtText:" not in automation_id:
                    continue
                rect = control.rectangle()
                if rect.width() < 20 or rect.height() < 10:
                    continue
                if rect.left < content_left or rect.right > settings_rect.right:
                    continue
                if rect.top < settings_rect.top + settings_rect.height() * 0.5:
                    continue
                candidates.append(control)
            except Exception:
                continue
        if not candidates:
            return None
        return max(candidates, key=lambda control: control.rectangle().top)

    def _meeting_has_finished(self) -> bool:
        if self.config.get("disable_meeting_end_text_detection"):
            return False
        if self.meeting_window is not None:
            try:
                handle = int(self.meeting_window.handle)
            except Exception:
                handle = 0
            if handle and not self._window_exists(handle):
                return self._finalize_meeting_end()
            if handle:
                if self._window_has_end_text(self.meeting_window):
                    return self._finalize_meeting_end()
                return False
        try:
            self._connect_window(timeout_seconds=1)
        except Exception:
            return self._finalize_meeting_end()
        if self._is_login_page():
            return False
        if self._has_text(["加入会议", "快速会议", "预定会议", "会议已结束"]):
            return self._finalize_meeting_end()
        return False

    def _finalize_meeting_end(self) -> bool:
        self._dismiss_after_meeting_dialog_if_present()
        return True

    def _dismiss_after_meeting_dialog_if_present(self) -> bool:
        """Dismiss Tencent Meeting's explicit post-meeting acknowledgement.

        The dialog is attached to the returned home window, and its visual label
        is not exposed.  Its Button automation id is stable, so never infer it
        from screen position or click an arbitrary acknowledgement button.
        """
        button = self._find_after_meeting_confirm_button()
        if button is None:
            return False
        try:
            button.invoke()
        except Exception:
            try:
                button.click_input()
            except Exception:
                return False
        return True

    def _find_after_meeting_confirm_button(self) -> Any | None:
        roots: list[Any] = [root for root in (self.window, self.meeting_window) if root is not None]
        try:
            from pywinauto import Desktop  # type: ignore

            roots.extend(Desktop(backend="uia").windows(visible_only=True))
        except Exception:
            pass
        seen: set[int] = set()
        for root in roots:
            try:
                handle = int(root.handle)
            except Exception:
                handle = id(root)
            if handle in seen:
                continue
            seen.add(handle)
            try:
                controls = [root, *root.descendants()]
            except Exception:
                continue
            for control in controls:
                try:
                    if str(control.element_info.automation_id or "") == self.AFTER_MEETING_CONFIRM_AUTOMATION_ID:
                        return control
                except Exception:
                    continue
        return None

    def _connect_window(self, timeout_seconds: int) -> None:
        self._ensure_pywinauto()
        from pywinauto import Application, Desktop  # type: ignore

        pattern = re.compile(self.window_title_regex)
        deadline = time.monotonic() + timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                for handle in self._find_window_handles(pattern):
                    window = Desktop(backend="uia").window(handle=handle)
                    if self._is_transient_window(window):
                        continue
                    self.window = window
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

    def _connect_in_meeting_window(self, timeout_seconds: int) -> bool:
        from pywinauto import Application, Desktop  # type: ignore

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for window in Desktop(backend="uia").windows():
                if not self._is_in_meeting_window(window):
                    continue
                self.window = window
                self.meeting_window = window
                try:
                    self.app = Application(backend="uia").connect(handle=window.handle)
                except Exception:
                    self.app = None
                self._bring_window_to_front()
                return True
            time.sleep(0.2)
        return False

    @staticmethod
    def _window_exists(handle: int) -> bool:
        try:
            import win32gui  # type: ignore

            return bool(win32gui.IsWindow(handle))
        except Exception:
            return False

    @staticmethod
    def _window_is_available(handle: int) -> bool:
        try:
            import win32gui  # type: ignore

            return bool(win32gui.IsWindow(handle) and win32gui.IsWindowVisible(handle))
        except Exception:
            return False

    def _capture_target_from_handle(self, handle: int) -> CaptureTarget:
        try:
            import win32gui  # type: ignore
            import win32process  # type: ignore

            title = str(win32gui.GetWindowText(handle) or "")
            class_name = str(win32gui.GetClassName(handle) or "")
            _, process_id = win32process.GetWindowThreadProcessId(handle)
            executable_name = self._process_executable_name(process_id)
        except Exception as exc:
            raise RuntimeError("recording_failed: failed to inspect Tencent Meeting window") from exc
        if not title or not class_name or not executable_name:
            raise RuntimeError("recording_failed: Tencent Meeting window identity is incomplete")
        return CaptureTarget(title, class_name, executable_name)

    @staticmethod
    def _process_executable_name(process_id: int) -> str:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, int(process_id)
        )
        if not handle:
            raise OSError(f"cannot open Tencent Meeting process {process_id}")
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                raise OSError(f"cannot query Tencent Meeting process {process_id}")
            return Path(buffer.value).name
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    @staticmethod
    def _window_has_end_text(window: Any) -> bool:
        try:
            texts = [str(control.window_text() or "") for control in window.descendants()]
        except Exception:
            return False
        return any(
            marker in text
            for text in texts
            for marker in ("会议已结束", "会议已被结束", "会议已关闭")
        )

    def _connect_join_window(self, timeout_seconds: int) -> bool:
        from pywinauto import Application, Desktop  # type: ignore

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            for window in Desktop(backend="uia").windows():
                if str(window.window_text() or "") not in {"加入会议", "Join Meeting"}:
                    continue
                self.window = window
                try:
                    self.app = Application(backend="uia").connect(handle=window.handle)
                except Exception:
                    self.app = None
                self._bring_window_to_front()
                return True
            time.sleep(0.2)
        return False

    def _wait_for_meeting_after_submit(self, meeting: MeetingInfo) -> bool:
        timeout_seconds = float(self.config.get("password_prompt_timeout_seconds") or 15)
        deadline = time.monotonic() + timeout_seconds
        password_submitted = False
        while time.monotonic() < deadline:
            if self._connect_in_meeting_window(timeout_seconds=0.5):
                return True
            if not self._connect_join_window(timeout_seconds=0.5):
                continue
            if self._has_text(["密码错误", "密码不正确", "会议密码错误"]):
                raise RuntimeError("Tencent Meeting password was rejected")
            password_edit = self._meeting_password_edit()
            if password_edit is not None and not password_submitted:
                if not meeting.password:
                    raise RuntimeError("Tencent Meeting requires a meeting password")
                self._set_login_edit_text(password_edit, meeting.password)
                self._click_first(["加入", "Join"])
                password_submitted = True
            time.sleep(0.2)
        return False

    def _meeting_password_edit(self) -> Any | None:
        for edit in self._edit_controls():
            try:
                automation_id = str(edit.element_info.automation_id or "")
            except Exception:
                continue
            if ".PwdEdit." in automation_id:
                return edit
        return None

    def _select_computer_audio_if_present(self) -> None:
        timeout_seconds = float(self.config.get("computer_audio_prompt_timeout_seconds") or 3)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._invoke_exact_button_if_present("使用电脑音频"):
                time.sleep(0.5)
                return
            time.sleep(0.2)

    def _invoke_exact_button_if_present(self, title: str) -> bool:
        if self.window is None:
            return False
        try:
            controls = self.window.descendants()
        except Exception:
            return False
        for control in controls:
            if str(control.window_text() or "") != title:
                continue
            if str(control.element_info.control_type or "") != "Button":
                continue
            try:
                if not control.is_visible() or not control.is_enabled():
                    continue
                control.invoke()
            except Exception:
                try:
                    control.click_input()
                except Exception:
                    return False
            return True
        return False

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
        return self._page_automation_id().startswith(self.LOGIN_GUIDE_PAGE) or self._has_text(
            ["我已阅读并同意", "手机号", "邮箱", "SSO", "企业微信", "微信登录"]
        )

    def _wait_for_login_guide_ready(self) -> None:
        timeout_seconds = float(self.config.get("login_form_timeout_seconds") or 15)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._has_text(["我已阅读并同意"]):
                return
            time.sleep(0.2)
        raise RuntimeError("Tencent Meeting login guide did not finish loading")

    def _click_phone_login_if_present(self) -> None:
        if self._click_if_present(["手机号"]):
            return
        entry = self._find_phone_login_entry()
        if entry is not None:
            rect = entry.rectangle()
            self._click_absolute(rect.left + rect.width() // 2, rect.top + rect.height() // 2)
            return
        self._click_ratio(self.config.get("phone_login_click_ratio") or [0.17, 0.76])

    def _find_phone_login_entry(self) -> Any | None:
        """Find the leftmost large login-method tile in Tencent Meeting's guide."""
        if self.window is None:
            return None
        candidates: list[Any] = []
        try:
            controls = self.window.descendants()
        except Exception:
            return None
        for control in controls:
            try:
                automation_id = str(control.element_info.automation_id or "")
                if ".NXQtHoverTipContainer:" not in automation_id:
                    continue
                rect = control.rectangle()
                if rect.width() >= 80 and rect.height() >= 100:
                    candidates.append(control)
            except Exception:
                continue
        if not candidates:
            return None
        return min(candidates, key=lambda control: control.rectangle().left)

    def _wait_for_phone_login_form(self) -> None:
        timeout_seconds = float(self.config.get("login_form_timeout_seconds") or 15)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                self._connect_window(timeout_seconds=1)
            except RuntimeError:
                time.sleep(0.2)
                continue
            if self._has_text(["为保障您的合法权益"]):
                self._dismiss_agreement_prompt_if_present()
                self._click_phone_login_if_present()
                time.sleep(0.2)
                continue
            if len(self._edit_controls()) >= 2 or self._has_text(
                ["密码登录", "验证码登录", "手机密码登录", "手机验证码登录"]
            ):
                return
            time.sleep(0.2)
        raise RuntimeError("Tencent Meeting phone login form did not appear")

    def _select_password_login(self) -> None:
        edits = self._edit_controls()
        if len(edits) >= 3 and str(edits[0].window_text() or "").strip() in {"86", "+86"}:
            return
        if self._has_text(["请输入密码", "忘记密码", "手机密码登录"]):
            return
        if self._click_if_present(["密码登录", "手机密码登录"]):
            time.sleep(0.5)
            return
        rect = self._text_rectangle("密码登录")
        if rect is not None:
            self._click_absolute(rect.left + rect.width() // 2, rect.top + rect.height() // 2)
            time.sleep(0.5)
            return
        raise RuntimeError("Tencent Meeting password login entry not found")

    def _wait_for_login_edits(self) -> None:
        timeout_seconds = float(self.config.get("login_form_timeout_seconds") or 15)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if len(self._edit_controls()) >= 2:
                return
            time.sleep(0.2)
        raise RuntimeError("Tencent Meeting login input controls not found")

    def _wait_until_login_complete(self) -> bool:
        timeout_seconds = float(self.config.get("login_result_timeout_seconds") or 12)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                self._connect_window(timeout_seconds=1)
            except RuntimeError:
                continue
            if self._has_text(["密码错误", "登录失败", "账号不可使用", "请求超时"]):
                raise RuntimeError("Tencent Meeting login was rejected")
            if not self._is_login_page() and len(self._edit_controls()) < 2:
                return True
        return False

    def _page_automation_id(self) -> str:
        if self.window is None:
            return ""
        try:
            return str(self.window.element_info.automation_id or "")
        except Exception:
            return ""

    def _dismiss_agreement_prompt_if_present(self) -> None:
        accept = self._find_agreement_accept_control()
        if accept is not None:
            rect = accept.rectangle()
            self._click_absolute(rect.left + rect.width() // 2, rect.top + rect.height() // 2)
            time.sleep(0.5)
            return
        rect = self._text_rectangle("为保障您的合法权益")
        if rect is None:
            return
        if self._invoke_exact_button_if_present("同意"):
            time.sleep(0.5)
            return
        self._click_ratio(self.config.get("agreement_prompt_accept_click_ratio") or [0.73, 0.55])
        time.sleep(0.5)

    def _find_agreement_accept_control(self) -> Any | None:
        """Find the rightmost action in the Tencent Meeting agreement alert."""
        if self.window is None:
            return None
        try:
            controls = self.window.descendants()
        except Exception:
            return None
        alert = next(
            (
                control
                for control in controls
                if str(getattr(control.element_info, "automation_id", "") or "")
                == "QApplication.wemeet://page/nxui/alert"
            ),
            None,
        )
        if alert is None:
            return None
        try:
            alert_rect = alert.rectangle()
        except Exception:
            return None
        actions: list[Any] = []
        for control in controls:
            try:
                automation_id = str(control.element_info.automation_id or "")
                if not automation_id.startswith("QApplication.wemeet://page/nxui/alert"):
                    continue
                if ".NXQtText:" not in automation_id:
                    continue
                rect = control.rectangle()
                if rect.top >= alert_rect.top + alert_rect.height() * 0.5 and rect.height() > 0:
                    actions.append(control)
            except Exception:
                continue
        if not actions:
            return None
        return max(actions, key=lambda control: control.rectangle().left)

    def _click_first(self, names: list[str]) -> None:
        for name in names:
            if self._invoke_exact_button_if_present(name):
                return
        if not self._click_if_present(names):
            raise RuntimeError(f"control not found: {names}")

    def _click_login(self) -> None:
        if self._click_if_present(["登录", "Login"]):
            return
        submit = self._find_login_submit_control()
        if submit is not None:
            rect = submit.rectangle()
            self._click_absolute(rect.left + rect.width() // 2, rect.top + rect.height() // 2)
            return
        self._click_ratio(self.config.get("login_click_ratio") or [0.5, 0.53])

    def _find_login_submit_control(self) -> Any | None:
        """Find the centered action immediately below the phone/password edits."""
        if self.window is None:
            return None
        edits = self._edit_controls()
        if len(edits) < 2:
            return None
        try:
            form_bottom = max(edit.rectangle().bottom for edit in edits)
            window_rect = self.window.rectangle()
            window_center = (window_rect.left + window_rect.right) / 2
            controls = self.window.descendants()
        except Exception:
            return None
        actions: list[Any] = []
        for control in controls:
            try:
                automation_id = str(control.element_info.automation_id or "")
                if ".NXQtText:" not in automation_id:
                    continue
                rect = control.rectangle()
                if rect.top <= form_bottom or rect.height() <= 0:
                    continue
                if abs(((rect.left + rect.right) / 2) - window_center) > window_rect.width() * 0.2:
                    continue
                actions.append(control)
            except Exception:
                continue
        if not actions:
            return None
        return min(actions, key=lambda control: control.rectangle().top)

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

        timeout_seconds = float(self.config.get("home_join_control_timeout_seconds") or 3)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            entry = self._find_home_join_entry()
            if entry is not None:
                rect = entry.rectangle()
                self._click_absolute(rect.left + rect.width() // 2, rect.top + rect.height() // 2)
                return
            time.sleep(0.2)
        raise RuntimeError("Tencent Meeting join entry control did not appear")

    def _find_home_join_entry(self) -> Any | None:
        """Find the first large quick-action icon in the Tencent Meeting home content."""
        if self.window is None:
            return None
        try:
            window_rect = self.window.rectangle()
            controls = self.window.descendants()
        except Exception:
            return None
        icons: list[Any] = []
        for control in controls:
            try:
                automation_id = str(control.element_info.automation_id or "")
                if ".NXQtImage:" not in automation_id:
                    continue
                rect = control.rectangle()
                # The quick-action icon scales with the window DPI.  Use its
                # proportion of the current window instead of a fixed pixel
                # range, which would silently fall back to a wrong coordinate
                # after a display-scale change.
                width_ratio = rect.width() / max(1, window_rect.width())
                height_ratio = rect.height() / max(1, window_rect.height())
                if not (0.04 <= width_ratio <= 0.12 and 0.07 <= height_ratio <= 0.16):
                    continue
                # The sidebar is a fixed narrow rail; quick actions begin in
                # the home content area to its right.
                if rect.left <= window_rect.left + window_rect.width() * 0.12:
                    continue
                icons.append(control)
            except Exception:
                continue
        if not icons:
            return None
        return min(icons, key=lambda control: (control.rectangle().top, control.rectangle().left))

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
    def _find_window_handles(pattern: re.Pattern[str]) -> list[int]:
        try:
            import win32gui  # type: ignore

            handles: list[int] = []

            def callback(hwnd: int, _lparam: object) -> bool:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd)
                if pattern.search(title or ""):
                    handles.append(hwnd)
                    return True
                if title == "腾讯会议":
                    handles.append(hwnd)
                return True

            win32gui.EnumWindows(callback, None)
            return handles
        except Exception:
            return []

    @classmethod
    def _find_window_handle(cls, pattern: re.Pattern[str]) -> int | None:
        handles = cls._find_window_handles(pattern)
        return handles[0] if handles else None

    @staticmethod
    def _is_transient_window(window: Any) -> bool:
        try:
            automation_id = str(window.element_info.automation_id or "").lower()
        except Exception:
            return False
        return "/page/nxui/toast" in automation_id or "/page/nxui/alert" in automation_id

    @classmethod
    def _is_in_meeting_window(cls, window: Any) -> bool:
        try:
            automation_id = str(window.element_info.automation_id or "")
        except Exception:
            return False
        return automation_id.startswith(cls.IN_MEETING_PAGE)

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
        phone_edit = self._find_edit_by_name(edits, "请输入手机号码")
        password_edit = self._find_edit_by_name(edits, "请输入密码")
        if phone_edit is not None and password_edit is not None:
            self._set_login_edit_text(phone_edit, credentials.account)
            self._set_login_edit_text(password_edit, credentials.password)
            return
        if len(edits) >= 3:
            self._set_login_edit_text(edits[1], credentials.account)
            self._set_login_edit_text(edits[2], credentials.password)
            return
        if len(edits) >= 2:
            self._set_login_edit_text(edits[0], credentials.account)
            self._set_login_edit_text(edits[1], credentials.password)
            return
        raise RuntimeError("login input controls not found")

    @staticmethod
    def _find_edit_by_name(edits: list[Any], expected_name: str) -> Any | None:
        for edit in edits:
            try:
                name = str(getattr(edit.element_info, "name", "") or "")
                if name == expected_name:
                    return edit
            except Exception:
                continue
        return None

    @staticmethod
    def _set_login_edit_text(edit: Any, text: str) -> None:
        edit.click_input()
        edit.type_keys("^a{BACKSPACE}", set_foreground=False)
        edit.type_keys(text, with_spaces=True, set_foreground=False, pause=0.03)
        time.sleep(0.2)

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
