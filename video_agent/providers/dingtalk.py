from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from video_agent.app_discovery import find_dingtalk_executable
from video_agent.models import CaptureTarget, Credentials, MeetingInfo
from video_agent.process_control import shutdown_matching_processes
from video_agent.providers.base import MeetingProvider


# -----------------------------------------------------------------------------
# Auto-IDs discovered from real DingTalk 7.x UI dumps.
# Stored as constants so a UI change shows up here as a single edit, and so
# future maintainers can grep them when the client ships a new build.
# -----------------------------------------------------------------------------

_LOGIN_TAB_AUTO_ID = (
    "loginView.widgetContainer.loginAccountPageView."
    "widgetSwitchModeBar.widgetLoginSwitchModeBar."
    "widgetAccountLogin.btnAccountMode"
)
_LOGIN_SWITCH_BAR_AUTO_ID = (
    "loginView.widgetContainer.loginAccountPageView.widgetSwitchModeBar"
)
_LOGIN_MOBILE_AUTO_ID = (
    "loginView.widgetContainer.loginAccountPageView.contentWidget."
    "contentWidgetInner.widgetCellInput.widgetContent.editMobile"
)
_LOGIN_PASSWORD_AUTO_ID = (
    "loginView.widgetContainer.loginAccountPageView.contentWidget."
    "contentWidgetInner.widgetPassword.widgetContent.editPassword"
)
_LOGIN_BUTTON_AUTO_ID = (
    "loginView.widgetContainer.loginAccountPageView.contentWidget."
    "contentWidgetInner.btnLogin"
)

# Main-window nav (left sidebar). Nav items are image-rendered (no usable text
# or auto_id), so we index into the ListItem children of NavigatorScrollView.
_NAV_BAR_AUTO_ID = (
    "dt_main_frame_view{default}.widget.splitter."
    "widgetNevigationBarContainer.navigator_view.NavigatorScrollView"
)

# Join dialog (separate top-level window that pops up when the user clicks
# the "加入会议" card). Its container is PrepareFrameV2.
_JOIN_WINDOW_TITLE_REGEX = "^钉钉视频会议$"
_MEETING_WINDOW_TITLE_REGEX = "^钉钉会议$|^钉钉视频会议$"
_JOIN_MEETING_NO_AUTO_ID = "PrepareFrameV2.widget.widget2_title.lineEdit_title"
_JOIN_START_BUTTON_AUTO_ID = (
    "PrepareFrameV2.widget.widget4_action.widget_start_conf.btn_start_conf"
)
_JOIN_PASSWORD_AUTO_ID = "PrepareFrameV2.FlexHintDialog.main_input"


class DingTalkProvider(MeetingProvider):
    provider_name = "dingtalk"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.app = None
        self.window = None
        self.meeting_window = None
        self._missing_meeting_window_polls = 0
        self.window_title_regex = str(
            config.get("window_title_regex") or "钉钉|DingTalk|Ding"
        )
        self.join_window_title_regex = str(
            config.get("join_window_title_regex") or _JOIN_WINDOW_TITLE_REGEX
        )
        self.meeting_window_title_regex = str(
            config.get("meeting_window_title_regex") or _MEETING_WINDOW_TITLE_REGEX
        )
        # Default index of the "会议" item inside the left nav scroll view.
        # 2026-07-10 verified empirically with find_meeting_index.py:
        # NavScrollView children (top→bottom) are
        # [文档, AI 表格, AI 听记, (hidden/empty), 会议, 日历, 待办, DING, DING]
        # so the "会议" item is index 4. The "消息" entry lives in a separate
        # top tab bar, not in this list. Tune per-machine if the client
        # introduces or hides more items.
        self.meeting_nav_index = int(config.get("meeting_nav_index") or 4)
        # Placeholder ratios; recalibrated against real dumps on 2026-07-09.
        # Re-tune per-machine if the DingTalk client moves things around.
        self.home_join_click_ratio = list(
            config.get("home_join_click_ratio") or [0.41, 0.26]
        )
        self.login_click_ratio = list(
            config.get("login_click_ratio") or [0.50, 0.68]
        )

    # ============================== public lifecycle ==============================

    def launch(self) -> None:
        try:
            self._connect_window(timeout_seconds=3)
            return
        except Exception:
            pass
        executable = find_dingtalk_executable(str(self.config.get("executable_path") or ""))
        if executable is None:
            raise RuntimeError("DingTalk executable not found")
        subprocess.Popen([str(executable)], cwd=str(executable.parent))
        # DingTalk may spend over a minute loading its embedded workbench on a
        # cold start before creating the native main window.
        self._connect_window(
            timeout_seconds=int(self.config.get("startup_timeout_seconds") or 90)
        )

    def ensure_logged_in(self, credentials: Credentials) -> None:
        self._ensure_pywinauto()
        login_retry_count = int(self.config.get("login_retry_count") or 2)
        for _ in range(login_retry_count):
            self._connect_window(timeout_seconds=5)
            if self._is_security_verification_page():
                raise RuntimeError("DingTalk login requires manual security verification")
            if not self._is_login_page():
                return
            self._click_account_password_login_if_present()
            time.sleep(1)
            self._fill_login_credentials(credentials)
            self._accept_agreement_if_present()
            self._click_login()
            time.sleep(4)
            if self._is_security_verification_page():
                raise RuntimeError("DingTalk login requires manual security verification")
        if self._is_login_page():
            raise RuntimeError("DingTalk login failed")

    def join(self, meeting: MeetingInfo) -> None:
        self._ensure_pywinauto()
        join_retry_count = int(self.config.get("join_retry_count") or 2)
        for attempt in range(join_retry_count):
            self._connect_window(timeout_seconds=5)
            if self._is_in_meeting_window(self.window):
                self.meeting_window = self.window
                self._missing_meeting_window_polls = 0
                return
            if self._is_login_page():
                raise RuntimeError("DingTalk is still on login page")
            if self._is_security_verification_page():
                raise RuntimeError("DingTalk login requires manual security verification")
            # Step 1: navigate from the DingTalk main window into the meeting app.
            self._navigate_to_meeting_home()
            # Step 2: click the "加入会议" card → opens the join dialog window.
            self._click_join_meeting_card()
            join_window = self._find_join_dialog_window(timeout_seconds=8)
            if join_window is None:
                try:
                    self._connect_window(timeout_seconds=2)
                except Exception:
                    pass
                if self._is_in_meeting_window(self.window):
                    self.meeting_window = self.window
                    return
                if attempt + 1 < join_retry_count:
                    continue
                raise RuntimeError("DingTalk join dialog window not found")
            # Step 3: fill the meeting number and submit.
            self._set_meeting_no(join_window, meeting.meeting_no)
            try:
                join_window_handle = int(join_window.handle)
            except Exception:
                join_window_handle = None
            self._click_start_conf(join_window)
            # A password-protected meeting shows a second prompt after the
            # meeting number is submitted.  Do not assume that a missing
            # meeting window means the join failed before checking that prompt.
            self.meeting_window = self._wait_for_meeting_or_password(
                meeting.password,
                float(self.config.get("password_prompt_timeout_seconds") or 15),
                preferred_handle=join_window_handle,
            )
            if self.meeting_window is not None:
                self._missing_meeting_window_polls = 0
                return
            if attempt + 1 < join_retry_count:
                continue
            raise RuntimeError("DingTalk meeting window not found after joining")
        raise RuntimeError("DingTalk join failed")

    def prepare_audio_video(self) -> None:
        self._ensure_pywinauto()
        # Only click controls whose current action is to turn a device off.
        # Clicking generic "microphone/camera" labels can invert an already
        # disabled device and accidentally enable it.
        target = self.meeting_window or self._find_join_dialog_window(timeout_seconds=2) or self.window
        if target is None:
            return
        self._click_exact_if_present_on(target, ["静音", "关闭麦克风"])
        self._click_exact_if_present_on(target, ["关闭摄像头", "关闭视频"])

    def wait_until_finished(self, deadline: datetime) -> None:
        poll_seconds = int(self.config.get("meeting_end_poll_seconds") or 5)
        while datetime.now(deadline.tzinfo).astimezone() < deadline:
            if self._meeting_has_finished():
                return
            time.sleep(poll_seconds)

    def get_capture_target(self) -> CaptureTarget | None:
        if self.meeting_window is None:
            raise RuntimeError("recording_failed: DingTalk meeting window is unavailable")
        try:
            handle = int(self.meeting_window.handle)
        except Exception as exc:
            raise RuntimeError("recording_failed: DingTalk meeting window handle is unavailable") from exc
        if not self._window_is_available(handle):
            raise RuntimeError("recording_failed: DingTalk meeting window is no longer visible")
        return self._capture_target_from_handle(handle)

    def capture_diagnostics(self, task_dir: Path) -> Path | None:
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / "diagnostic.txt"
        lines = ["DingTalk diagnostics"]
        try:
            self._connect_window(timeout_seconds=1)
            if self.window is not None:
                lines.append(str(self.window.window_text()))
                lines.append(str(self.window.texts()))
                lines.extend(self._control_texts())
        except Exception as exc:
            lines.append(f"diagnostic_error={exc}")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def cleanup(self) -> None:
        self.meeting_window = None
        self.window = None
        self._missing_meeting_window_polls = 0

    def shutdown_application(self) -> None:
        """Leave the active meeting and close all verified DingTalk processes."""
        self._leave_meeting_if_present()
        executable = find_dingtalk_executable(
            str(self.config.get("executable_path") or "")
        )
        if executable is None:
            return
        install_root = self._dingtalk_install_root(executable)
        shutdown_matching_processes(
            executable_names={"DingtalkLauncher.exe", "DingTalk.exe", "tblive.exe"},
            allowed_roots={install_root},
            timeout_seconds=5,
        )

    def _leave_meeting_if_present(self) -> None:
        target = self.meeting_window or self.window
        if not self._is_in_meeting_window(target):
            return
        try:
            if not self._click_if_present_on(target, ["结束"]):
                return
            time.sleep(0.5)
            self._click_if_present_on(
                target, ["离开会议", "确认离开", "离开", "结束会议"]
            )
            time.sleep(0.5)
        except Exception:
            return

    @staticmethod
    def _dingtalk_install_root(executable: Path) -> Path:
        resolved = executable.resolve()
        for candidate in (resolved.parent, *resolved.parents):
            if candidate.name.casefold() in {"dingding", "dingtalk"}:
                return candidate
        return resolved.parent

    # ============================== meeting end detection ==============================

    def _meeting_has_finished(self) -> bool:
        if self.config.get("disable_meeting_end_text_detection"):
            return False
        # The saved meeting window is authoritative while it remains visible.
        saved_window_missing = self.meeting_window is not None
        if self.meeting_window is not None:
            try:
                handle = int(self.meeting_window.handle)
            except Exception:
                handle = 0
            if handle and self._window_is_available(handle):
                saved_window_missing = False
                if self._window_has_text(
                    self.meeting_window, ["会议已结束", "返回首页"]
                ):
                    return True
                if self._is_in_meeting_window(self.meeting_window):
                    self._missing_meeting_window_polls = 0
                    return False

        # A DingTalk transition can briefly invalidate the UIA wrapper. Rebind
        # once before deciding that the meeting window disappeared.
        rebound = self._find_meeting_window(timeout_seconds=1)
        if rebound is not None and self._is_in_meeting_window(rebound):
            self.meeting_window = rebound
            self._missing_meeting_window_polls = 0
            return False

        # DingTalk may close both the meeting window and the main client when
        # a meeting ends.  After two consecutive absent-window polls (normally
        # ten seconds), treat that as a confirmed end rather than recording
        # until the scheduled deadline.  One miss still falls through to the
        # normal homepage check so a transient UIA failure cannot stop a live
        # meeting.
        if saved_window_missing:
            self._missing_meeting_window_polls += 1
        else:
            self._missing_meeting_window_polls = 0

        # Only finish early when the main client is reachable and shows a
        # meeting-home/end state. Transient UI automation failures fall back to
        # the planned deadline instead of stopping the recording prematurely.
        try:
            self._connect_window(timeout_seconds=1)
        except Exception:
            return self._missing_meeting_window_polls >= 2
        if self._is_login_page():
            return False
        if self._has_text(["加入会议", "发起会议", "预约会议", "会议已结束"]):
            return True
        return self._missing_meeting_window_polls >= 2

    # ============================== join dialog helpers ==============================

    def _navigate_to_meeting_home(self) -> None:
        """Click the meeting-app nav item (left sidebar) to load the meeting work area."""
        if self.window is None:
            return
        try:
            scroll_view = self.window.child_window(auto_id=_NAV_BAR_AUTO_ID)
            items = scroll_view.children()
            if not items or len(items) <= self.meeting_nav_index:
                raise RuntimeError("meeting nav item out of range")
            rect = items[self.meeting_nav_index].rectangle()
            cx = (rect.left + rect.right) // 2
            cy = (rect.top + rect.bottom) // 2
            self._bring_window_to_front()
            self._native_click(cx, cy)
            time.sleep(2.0)  # let DingTalk switch + load the webview
        except Exception:
            # Fallback ratio: roughly index-aligned within the nav strip.
            rect = self.window.rectangle()
            self._native_click(
                int(rect.left + rect.width() * 0.10),
                int(rect.top + rect.height() * (0.15 + 0.045 * self.meeting_nav_index)),
            )
            time.sleep(2.0)

    def _click_join_meeting_card(self) -> None:
        """Click the '加入会议' card on the meeting app home page."""
        if self.window is None:
            raise RuntimeError("DingTalk window is not connected")
        # Prefer a text-based selector inside the work area.
        for ctrl in self.window.descendants():
            try:
                if ctrl.window_text() == "加入会议":
                    r = ctrl.rectangle()
                    # The card label is a small button-sized label, not the
                    # whole 238x175 card. Filter to the inner label.
                    if 60 < r.width() < 200 and 20 < r.height() < 60:
                        self._bring_window_to_front()
                        self._native_click(
                            (r.left + r.right) // 2, (r.top + r.bottom) // 2
                        )
                        time.sleep(2.5)
                        return
            except Exception:
                continue
        # Fallback: ratio against the main window.
        rect = self.window.rectangle()
        self._bring_window_to_front()
        self._native_click(
            int(rect.left + rect.width() * self.home_join_click_ratio[0]),
            int(rect.top + rect.height() * self.home_join_click_ratio[1]),
        )
        time.sleep(2.5)

    def _find_join_dialog_window(self, timeout_seconds: int) -> Any | None:
        return self._find_titled_meeting_window(timeout_seconds, expect_prepare=True)

    def _find_meeting_window(
        self, timeout_seconds: int, preferred_handle: int | None = None
    ) -> Any | None:
        # After the submit click, DingTalk commonly keeps the preparation
        # controls in the UIA tree while reusing the same native window for the
        # meeting. At that point the phase and handle are more reliable than
        # the stale visibility of the start button.
        return self._find_titled_meeting_window(
            timeout_seconds,
            expect_prepare=None,
            preferred_handle=preferred_handle,
        )

    def _wait_for_meeting_or_password(
        self,
        password: str,
        timeout_seconds: float,
        preferred_handle: int | None = None,
    ) -> Any | None:
        """Wait for either the actual meeting or DingTalk's password prompt."""
        deadline = time.monotonic() + max(timeout_seconds, 1)
        password_submitted = not bool(password)
        while time.monotonic() < deadline:
            if self._password_error_message() is not None:
                raise RuntimeError("DingTalk meeting password rejected")
            if not password_submitted:
                prompt = self._find_password_prompt()
                if prompt is not None:
                    self._submit_meeting_password(prompt, password)
                    password_submitted = True
                    # Let DingTalk replace the password form before treating
                    # any same-title window as the actual meeting.
                    time.sleep(0.25)
                    continue
            # DingTalk can reuse the preparation window for its meeting UI, so
            # retain the submitted window handle as the preferred candidate.
            meeting_window = self._find_meeting_window(
                timeout_seconds=0.1, preferred_handle=preferred_handle
            )
            if meeting_window is not None and (
                self._is_in_meeting_window(meeting_window)
                or not self._is_prepare_window(meeting_window)
            ):
                return meeting_window
            # The UIA tree may briefly still describe the preparation window
            # immediately after submission.  Poll rather than treating it as a
            # meeting window until it exposes real in-meeting controls.
            time.sleep(0.25)
        return None

    def _find_password_prompt(self) -> Any | None:
        """Find a meeting-password prompt without mistaking it for login."""
        for window in self._visible_titled_meeting_windows():
            values = "\n".join(self._window_text_values(window))
            if not any(
                marker in values
                for marker in ("会议密码", "入会密码", "会议口令", "请输入密码")
            ):
                continue
            if self._password_edit_control(window) is not None:
                return window
        return None

    def _submit_meeting_password(self, prompt: Any, password: str) -> None:
        edit = self._password_edit_control(prompt)
        if edit is None:
            raise RuntimeError("DingTalk meeting password input not found")
        self._set_edit_text(edit, password)
        if not self._click_exact_if_present_on(prompt, ["确定", "加入会议", "加入", "入会"]):
            raise RuntimeError("DingTalk meeting password submit button not found")

    def _password_error_message(self) -> str | None:
        for window in self._visible_titled_meeting_windows():
            values = "\n".join(self._window_text_values(window))
            if any(marker in values for marker in ("密码错误", "密码不正确", "密码无效")):
                return "DingTalk meeting password rejected"
        return None

    @staticmethod
    def _password_edit_control(window: Any) -> Any | None:
        """Return the password edit from either a native or rendered prompt."""
        # In DingTalk 7.x the password modal is embedded in PrepareFrameV2.
        # That parent also contains the original meeting-number edit, so a
        # generic "last Edit" selection would overwrite the meeting number.
        try:
            edit = window.child_window(auto_id=_JOIN_PASSWORD_AUTO_ID)
            _ = edit.window_text()
            return edit
        except Exception:
            pass
        try:
            controls = list(window.descendants())
        except Exception:
            return None
        edits: list[Any] = []
        for control in controls:
            try:
                info = getattr(control, "element_info", None)
                control_type = str(getattr(info, "control_type", "")).casefold()
                class_name = str(getattr(info, "class_name", "")).casefold()
                if control_type == "edit" or "lineedit" in class_name:
                    edits.append(control)
            except Exception:
                continue
        return edits[-1] if edits else None

    def _visible_titled_meeting_windows(self) -> list[Any]:
        """Return visible DingTalk meeting windows, including transient prompts."""
        try:
            from pywinauto import Application, Desktop  # type: ignore

            desktop = Desktop(backend="uia")
            candidates: list[Any] = []
            for window in desktop.windows(
                title_re=self.meeting_window_title_regex, visible_only=True
            ):
                try:
                    handle = int(window.handle)
                    app = Application(backend="uia").connect(handle=handle)
                    candidates.append(app.window(handle=handle))
                except Exception:
                    continue
            return candidates
        except Exception:
            return []

    def _find_titled_meeting_window(
        self,
        timeout_seconds: int,
        expect_prepare: bool | None,
        preferred_handle: int | None = None,
    ) -> Any | None:
        try:
            from pywinauto import Application, Desktop  # type: ignore

            desktop = Desktop(backend="uia")
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                try:
                    title_regex = self.join_window_title_regex if expect_prepare is True else self.meeting_window_title_regex
                    candidates = self._visible_titled_meeting_windows_for_regex(
                        desktop, Application, title_regex
                    )
                    selected = self._select_titled_window(
                        candidates, expect_prepare, preferred_handle
                    )
                    if selected is not None:
                        return selected
                except Exception:
                    pass
                time.sleep(0.5)
        except Exception:
            return None

    @staticmethod
    def _visible_titled_meeting_windows_for_regex(
        desktop: Any, application: Any, title_regex: str
    ) -> list[Any]:
        candidates: list[Any] = []
        for window in desktop.windows(title_re=title_regex, visible_only=True):
            try:
                handle = int(window.handle)
                app = application(backend="uia").connect(handle=handle)
                candidates.append(app.window(handle=handle))
            except Exception:
                continue
        return candidates

    @classmethod
    def _select_titled_window(
        cls,
        candidates: list[Any],
        expect_prepare: bool | None,
        preferred_handle: int | None = None,
    ) -> Any | None:
        usable = [
            candidate
            for candidate in candidates
            if not cls._window_has_text(candidate, ["会议已结束", "返回首页"])
        ]
        if preferred_handle is not None:
            for candidate in usable:
                try:
                    if int(candidate.handle) == preferred_handle:
                        return candidate
                except Exception:
                    continue
        if expect_prepare is None:
            return usable[0] if usable else None
        for candidate in usable:
            is_prepare = cls._is_prepare_window(candidate)
            if is_prepare is expect_prepare:
                return candidate
        return None

    @staticmethod
    def _is_prepare_window(window: Any) -> bool:
        try:
            button = window.child_window(auto_id=_JOIN_START_BUTTON_AUTO_ID)
            return bool(button.is_visible())
        except Exception:
            return False

    @classmethod
    def _is_in_meeting_window(cls, window: Any) -> bool:
        if window is None:
            return False
        values = cls._window_text_values(window)
        current = "\n".join(values)
        return "会议信息" in current and any(
            marker in current for marker in ("结束", "成员", "共享")
        )

    @staticmethod
    def _window_has_text(window: Any, texts: list[str]) -> bool:
        current = "\n".join(DingTalkProvider._window_text_values(window))
        return any(text in current for text in texts)

    @staticmethod
    def _window_text_values(window: Any) -> list[str]:
        values: list[str] = []
        try:
            values.extend(str(item) for item in window.texts())
        except Exception:
            pass
        try:
            values.extend(str(item.window_text() or "") for item in window.descendants())
        except Exception:
            pass
        return values

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
            raise RuntimeError("recording_failed: failed to inspect DingTalk meeting window") from exc
        if not title or not class_name or not executable_name:
            raise RuntimeError("recording_failed: DingTalk meeting window identity is incomplete")
        return CaptureTarget(
            title=title,
            class_name=class_name,
            executable_name=executable_name,
        )

    @staticmethod
    def _process_executable_name(process_id: int) -> str:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information, False, int(process_id)
        )
        if not handle:
            raise OSError(f"cannot open DingTalk process {process_id}")
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buffer, ctypes.byref(size)
            ):
                raise OSError(f"cannot query DingTalk process {process_id}")
            return Path(buffer.value).name
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    def _set_meeting_no(self, join_window: Any, meeting_no: str) -> None:
        try:
            edit = join_window.child_window(auto_id=_JOIN_MEETING_NO_AUTO_ID)
            self._set_edit_text(edit, meeting_no)
        except Exception as exc:
            raise RuntimeError(
                "DingTalk join dialog meeting number input not found"
            ) from exc

    def _click_start_conf(self, join_window: Any) -> None:
        try:
            btn = join_window.child_window(auto_id=_JOIN_START_BUTTON_AUTO_ID)
            rect = btn.rectangle()
        except Exception as exc:
            raise RuntimeError(
                "DingTalk join dialog start button not found"
            ) from exc
        try:
            join_window.set_focus()
        except Exception:
            pass
        self._native_click(
            (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
        )

    # ============================== login helpers ==============================

    def _click_account_password_login_if_present(self) -> None:
        if self._click_auto_id_if_present(_LOGIN_TAB_AUTO_ID):
            return
        if self._click_if_present(
            ["密码登录", "账号密码登录", "使用密码登录", "账号登录"]
        ):
            return
        if self._click_if_present(["手机号登录"]):
            return
        if self.config.get("password_login_click_ratio"):
            self._click_ratio(self.config["password_login_click_ratio"])

    def _click_auto_id_if_present(self, auto_id: str) -> bool:
        if self.window is None:
            return False
        try:
            ctrl = self.window.child_window(auto_id=auto_id)
            ctrl.click_input()
            return True
        except Exception:
            return False

    def _fill_login_credentials(self, credentials: Credentials) -> None:
        if self.window is None:
            raise RuntimeError("DingTalk window is not connected")
        try:
            mobile = self.window.child_window(auto_id=_LOGIN_MOBILE_AUTO_ID)
            password = self.window.child_window(auto_id=_LOGIN_PASSWORD_AUTO_ID)
        except Exception as exc:
            raise RuntimeError("DingTalk login input controls not found") from exc
        self._set_edit_text(mobile, credentials.account)
        self._set_edit_text(password, credentials.password)

    def _click_login(self) -> None:
        if self.window is not None:
            try:
                btn = self.window.child_window(auto_id=_LOGIN_BUTTON_AUTO_ID)
                rect = btn.rectangle()
                self._bring_window_to_front()
                self._native_click(
                    (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
                )
                return
            except Exception:
                pass
        if self._click_if_present(["登录", "Login"]):
            return
        self._click_ratio(self.login_click_ratio)

    # ============================== window + generic helpers ==============================

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
        raise RuntimeError("DingTalk window not found") from last_error

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
        # Prefer auto-id detection: the login page has the tab-switch bar
        # with btnAccountMode/btnQRCodeMode children.
        if self.window is not None:
            try:
                bar = self.window.child_window(auto_id=_LOGIN_SWITCH_BAR_AUTO_ID)
                # Bar exists if we can resolve it. Try a no-op text read.
                _ = bar.window_text()
                return True
            except Exception:
                pass
        # Fallback to text matching (handles unusual locales or older builds).
        return self._has_text(
            [
                "请输入手机号",
                "请输入账号",
                "请输入密码",
                "验证码登录",
                "扫码登录",
                "密码登录",
                "你好",
            ]
        )

    def _is_security_verification_page(self) -> bool:
        return self._has_text(
            [
                "请输入验证码",
                "短信验证码",
                "安全验证",
                "扫码确认",
                "设备验证",
                "人脸验证",
                "二次验证",
            ]
        )

    def _accept_agreement_if_present(self) -> None:
        if not self._has_text(["我已阅读并同意", "同意服务协议"]):
            return
        if self._click_if_present(["我已阅读并同意", "同意服务协议"]):
            return
        rect = self._text_rectangle("我已阅读并同意") or self._text_rectangle(
            "同意服务协议"
        )
        if rect is not None:
            self._click_absolute(rect.left - 18, rect.top + rect.height() // 2)
            return
        if self.config.get("agreement_click_ratio"):
            self._click_ratio(self.config["agreement_click_ratio"])

    def _click_if_present(self, names: list[str]) -> bool:
        return self._click_if_present_on(self.window, names)

    @staticmethod
    def _click_if_present_on(target: Any, names: list[str]) -> bool:
        if target is None:
            return False
        for name in names:
            try:
                target.child_window(
                    title_re=f".*{re.escape(name)}.*"
                ).click_input()
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _click_exact_if_present_on(target: Any, names: list[str]) -> bool:
        if target is None:
            return False
        expected = set(names)
        try:
            controls = target.descendants()
        except Exception:
            return False
        for control in controls:
            try:
                if str(control.window_text() or "").strip() in expected:
                    control.click_input()
                    return True
            except Exception:
                continue
        return False

    def _click_ratio(self, ratio: Any) -> None:
        if self.window is None:
            raise RuntimeError("DingTalk window is not connected")
        self._bring_window_to_front()
        if not isinstance(ratio, list | tuple) or len(ratio) != 2:
            raise RuntimeError("click ratio must be a two-item list")
        left, top, width, height = self._window_bounds()
        self._native_click(
            int(left + width * float(ratio[0])), int(top + height * float(ratio[1]))
        )

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
            raise RuntimeError("DingTalk window is not connected")
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

            exact_main_handles: list[int] = []
            matching_handles: list[int] = []

            def callback(hwnd: int, _lparam: object) -> bool:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd)
                # A meeting window also matches the broad DingTalk pattern.
                # Prefer the main client explicitly when reconnecting for an
                # end-state check, otherwise the stale meeting wrapper masks
                # the home screen behind it.
                if title == "钉钉":
                    exact_main_handles.append(hwnd)
                elif pattern.search(title or ""):
                    matching_handles.append(hwnd)
                return True

            win32gui.EnumWindows(callback, None)
            return (exact_main_handles or matching_handles or [None])[0]
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

    # ============================== edit/text-input helpers ==============================

    @staticmethod
    def _set_edit_text(edit: Any, text: str) -> None:
        try:
            edit.set_edit_text("")
            edit.set_edit_text(text)
            return
        except Exception:
            pass
        # Fallback for webview/rendered edits that ignore set_edit_text.
        try:
            edit.click_input()
            edit.type_keys("^a{BACKSPACE}", with_spaces=True, set_foreground=False)
            time.sleep(0.2)
            edit.type_keys(text, with_spaces=True, set_foreground=False)
            return
        except Exception:
            pass
        edit.click_input()
        time.sleep(0.2)
        edit.type_keys("^a{DEL}", with_spaces=True, set_foreground=False)
        time.sleep(0.2)
        edit.type_keys(text, with_spaces=True, set_foreground=False)

    @staticmethod
    def _ensure_pywinauto() -> None:
        try:
            import pywinauto  # noqa: F401
        except ModuleNotFoundError as exc:
            raise RuntimeError("pywinauto is not installed; run pip install -r requirements.txt") from exc
