from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from video_agent.app_discovery import find_mixlink_executable
from video_agent.models import CaptureTarget, Credentials, MeetingInfo
from video_agent.process_control import _list_processes, _query_process_path, shutdown_matching_processes
from video_agent.providers.base import MeetingProvider


class MixLinkProvider(MeetingProvider):
    """Automation for the Windows MixLink (觅讯) desktop client."""

    provider_name = "mixlink"
    MAIN_CLASS = "easylink::EzEasyLink"
    LOGIN_CLASS = "usermanagement::StartUpWindows"
    PROCESS_NAMES = {"EzEasyLink.exe", "EzEasyLinkWeb.exe", "EZMeeting.exe"}

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.executable: Path | None = None
        self.main_window = None
        self.window = None
        self.meeting_window = None
        self._startup_observations: list[str] = []
        self._join_observations: list[str] = []
        self._side_panel_observations: set[str] = set()
        self._last_process_snapshot = ""

    def launch(self) -> None:
        self._ensure_pywinauto()
        self.executable = find_mixlink_executable(str(self.config.get("executable_path") or ""))
        if self._connect_main_window(2, required=False):
            return
        if self.executable is None:
            raise RuntimeError("MixLink executable not found")
        startup_timeout = float(self.config.get("startup_timeout_seconds") or 45)
        self._startup_observations = []
        self._last_process_snapshot = ""
        self._record_startup_state("before_launch")
        subprocess.Popen([str(self.executable)], cwd=str(self.executable.parent))
        if not self._connect_main_window(startup_timeout, required=False):
            self._record_startup_state("timeout")
            raise RuntimeError(
                f"MixLink main window not found after waiting {startup_timeout:g} seconds"
            )

    def ensure_logged_in(self, credentials: Credentials) -> None:
        if self._main_is_logged_in():
            return
        retries = int(self.config.get("login_retry_count") or 2)
        for _ in range(retries):
            self._connect_main_window(5)
            if self._main_is_logged_in():
                return
            login = self._open_login_dialog()
            if login is None:
                continue
            self.window = login
            self._click_named(login, ["密码登录"])
            edits = list(login.descendants(control_type="Edit"))
            if len(edits) < 2:
                raise RuntimeError("MixLink login input controls not found")
            self._set_edit_text(edits[0], credentials.account)
            self._set_edit_text(edits[1], credentials.password)
            self._accept_login_agreement(login)
            login_button = self._find_by_auto_id_suffix(login, "logInBtn")
            if login_button is not None:
                self._activate_control(login_button)
            elif not self._click_named(login, ["登录"]):
                raise RuntimeError("MixLink login button not found")
            deadline = time.monotonic() + float(self.config.get("login_success_timeout_seconds") or 15)
            while time.monotonic() < deadline:
                self._connect_main_window(1, required=False)
                if self._main_is_logged_in():
                    self.window = self.main_window
                    return
                time.sleep(0.5)
        raise RuntimeError("MixLink login failed or requires manual verification")

    def join(self, meeting: MeetingInfo) -> None:
        self._connect_main_window(5)
        if not self._main_is_logged_in():
            raise RuntimeError("MixLink is still on login page")
        retries = int(self.config.get("join_retry_count") or 2)
        self._join_observations = []
        for _ in range(retries):
            join_window = self._open_join_dialog()
            self._record_join_state(
                "join_dialog_found" if join_window is not None else "join_dialog_not_found"
            )
            if join_window is None:
                continue
            self.window = join_window
            guide = self._find_by_auto_id_suffix(join_window, "btnKnow")
            if guide is not None:
                try:
                    self._activate_control(guide)
                    time.sleep(0.25)
                except Exception:
                    pass
            edits = list(join_window.descendants(control_type="Edit"))
            if not edits:
                raise RuntimeError("MixLink meeting number input not found")
            self._set_edit_text(edits[0], meeting.meeting_no)
            join_button = self._find_by_auto_id_suffix(
                join_window, "wgtBottom.wgtBtnJoin.pushButton"
            )
            if join_button is not None:
                self._activate_control(join_button)
            elif not self._click_named(join_window, ["加入会议", "加入", "入会"]):
                raise RuntimeError("MixLink join button not found")
            self._record_join_state("join_submitted")
            meeting_window = self._wait_for_meeting_or_password(
                meeting.password,
                float(self.config.get("password_prompt_timeout_seconds") or 15),
            )
            self._record_join_state(
                "meeting_window_found" if meeting_window is not None else "meeting_window_not_found"
            )
            if meeting_window is not None:
                self.meeting_window = meeting_window
                self.window = meeting_window
                return
        raise RuntimeError("MixLink meeting window not found after joining")

    def prepare_audio_video(self) -> None:
        if self.meeting_window is None:
            raise RuntimeError("MixLink meeting window is unavailable")
        # Only click controls whose text explicitly means the device is on.
        # Text such as “开启麦克风” already means it is off and must be left alone.
        self._click_exact_named(self.meeting_window, ["关闭麦克风", "静音"])
        self._click_exact_named(self.meeting_window, ["关闭摄像头", "关闭视频", "停止视频"])
        self._collapse_side_panels(self.meeting_window)

    def get_capture_target(self) -> CaptureTarget | None:
        handle = self._window_handle(self.meeting_window)
        if not handle or not self._is_window_visible(handle):
            self.meeting_window = self._find_meeting_window()
            handle = self._window_handle(self.meeting_window)
        if not handle:
            raise RuntimeError("recording_failed: MixLink meeting window is unavailable")
        title, class_name, executable_name = self._window_identity(handle)
        if not title or not class_name or executable_name.casefold() not in {
            "ezmeeting.exe", "ezeasylink.exe"
        }:
            raise RuntimeError("recording_failed: invalid MixLink meeting window")
        return CaptureTarget(title, class_name, executable_name)

    def wait_until_finished(self, deadline: datetime) -> None:
        poll_seconds = float(self.config.get("meeting_end_poll_seconds") or 5)
        missing_count = 0
        while datetime.now(deadline.tzinfo).astimezone() < deadline:
            # MixLink normally shows this rating modal after a meeting ends.
            # It can block UIA access to the home page, so treat it as the
            # definitive end signal before checking the saved meeting window.
            if self._has_finished_dialog():
                return
            current = self._find_meeting_window()
            if current is not None:
                self.meeting_window = current
                if self._meeting_has_ended_text(current):
                    return
                # Meeting notes/transcription can reopen after the client
                # receives an update.  Only repeat the same narrowly scoped,
                # verified close action; never use a generic window-level X.
                self._collapse_side_panels(current)
                missing_count = 0
            else:
                # A destroyed native meeting window is a definitive end signal.
                # It also covers the common MixLink behavior of closing the
                # client instead of returning to its home page.
                handle = self._window_handle(self.meeting_window)
                if handle and not self._window_exists(handle):
                    return
                missing_count += 1
                if missing_count >= 2 and self._main_home_is_visible():
                    return
            time.sleep(poll_seconds)

    def shutdown_application(self) -> None:
        executable = self.executable or find_mixlink_executable(
            str(self.config.get("executable_path") or "")
        )
        if executable is None:
            return
        shutdown_matching_processes(
            executable_names=self.PROCESS_NAMES,
            allowed_roots={executable.parent},
            timeout_seconds=float(self.config.get("shutdown_timeout_seconds") or 5),
        )

    def capture_diagnostics(self, task_dir: Path) -> Path | None:
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / "mixlink-diagnostic.txt"
        lines = ["MixLink diagnostics"]
        lines.extend(self._startup_observations)
        lines.extend(self._join_observations)
        for label, window in (("main", self.main_window), ("current", self.window), ("meeting", self.meeting_window)):
            try:
                lines.append(f"{label}: title={window.window_text()!r} class={window.class_name()!r} handle={window.handle}")
                lines.extend(f"  {text}" for text in self._texts(window))
            except Exception as exc:
                lines.append(f"{label}: unavailable ({exc})")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def cleanup(self) -> None:
        self.main_window = None
        self.window = None
        self.meeting_window = None
        self._side_panel_observations.clear()

    def _connect_main_window(self, timeout_seconds: float, required: bool = True) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            window = self._find_window_by_class(self.MAIN_CLASS)
            if window is not None:
                self.main_window = window
                self.window = window
                self._bring_to_front(window)
                return True
            self._record_startup_state("waiting_for_main_window")
            time.sleep(0.5)
        if required:
            raise RuntimeError("MixLink main window not found")
        return False

    def _main_is_logged_in(self) -> bool:
        if self.main_window is None:
            return False
        text = "\n".join(self._texts(self.main_window))
        return "请登录" not in text and "加入会议" in text

    def _open_login_dialog(self) -> Any | None:
        if self.main_window is None:
            raise RuntimeError("MixLink main window is not connected")
        control = self._find_by_auto_id_suffix(self.main_window, "goLoginPshBtn")
        if control is None:
            control = self._find_text_control(self.main_window, "请登录")
        if control is None:
            raise RuntimeError("MixLink login entry not found")

        try:
            control.click_input()
        except Exception:
            pass
        login = self._wait_for_window_class(self.LOGIN_CLASS, 2)
        if login is not None:
            return login

        try:
            control.click()
        except Exception:
            pass
        login = self._wait_for_window_class(self.LOGIN_CLASS, 2)
        if login is not None:
            return login

        try:
            rect = control.rectangle()
            self._native_click(rect.left + rect.width() // 2, rect.top + rect.height() // 2)
        except Exception:
            pass
        return self._wait_for_window_class(self.LOGIN_CLASS, 3)

    def _open_join_dialog(self) -> Any | None:
        """Open the Qt card using progressively more direct click mechanisms."""
        if self.main_window is None:
            raise RuntimeError("MixLink main window is not connected")
        control = self._find_by_auto_id_suffix(self.main_window, "joinWidget")
        if control is None:
            control = self._find_text_control(self.main_window, "加入会议")
        if control is None:
            raise RuntimeError("MixLink join entry not found")

        try:
            control.click_input()
        except Exception:
            pass
        dialog = self._wait_for_join_window(2)
        if dialog is not None:
            return dialog

        try:
            control.click()
        except Exception:
            pass
        dialog = self._wait_for_join_window(2)
        if dialog is not None:
            return dialog

        try:
            rect = control.rectangle()
            self._native_click(rect.left + rect.width() // 2, rect.top + rect.height() // 2)
        except Exception:
            pass
        return self._wait_for_join_window(3)

    def _find_join_window(self) -> Any | None:
        for window in self._process_windows(visible_only=True):
            text = "\n".join(self._texts(window))
            if "加入会议" in text and list(window.descendants(control_type="Edit")):
                return window
        return None

    def _wait_for_join_window(self, timeout_seconds: float) -> Any | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            window = self._find_join_window()
            if window is not None:
                return window
            time.sleep(0.25)
        return None

    def _wait_for_meeting_or_password(
        self, password: str, timeout_seconds: float
    ) -> Any | None:
        deadline = time.monotonic() + timeout_seconds
        password_submitted = not bool(password)
        while time.monotonic() < deadline:
            if self._password_error_message() is not None:
                self._record_join_state("password_rejected")
                raise RuntimeError("MixLink meeting password rejected")
            meeting_window = self._find_meeting_window()
            if meeting_window is not None:
                return meeting_window
            if not password_submitted:
                prompt = self._find_password_prompt()
                if prompt is not None:
                    self._record_join_state("password_prompt_found")
                    self._submit_meeting_password(prompt, password)
                    password_submitted = True
                    self._record_join_state("password_submitted")
            time.sleep(0.25)
        return None

    def _find_password_prompt(self) -> Any | None:
        for window in self._process_windows(visible_only=True):
            text = "\n".join(self._texts(window))
            try:
                if window.class_name() == self.LOGIN_CLASS:
                    continue
            except Exception:
                continue
            if not any(marker in text for marker in ("会议密码", "入会密码", "会议口令", "请输入密码")):
                continue
            edits = list(window.descendants(control_type="Edit"))
            if edits:
                return window
        return None

    def _submit_meeting_password(self, prompt: Any, password: str) -> None:
        edits = list(prompt.descendants(control_type="Edit"))
        if not edits:
            raise RuntimeError("MixLink meeting password input not found")
        self._set_edit_text(edits[-1], password)
        if not self._click_exact_named(prompt, ["确定", "加入会议", "加入"]):
            raise RuntimeError("MixLink meeting password submit button not found")

    def _password_error_message(self) -> str | None:
        for window in self._process_windows(visible_only=True):
            text = "\n".join(self._texts(window))
            if any(marker in text for marker in ("密码错误", "密码不正确", "密码无效")):
                return "MixLink meeting password rejected"
        return None

    def _accept_login_agreement(self, login: Any) -> None:
        button = self._find_by_auto_id_suffix(login, "sureChkbx.mnBtn")
        if button is None:
            button = self._find_login_agreement_control(login)
        if button is None:
            raise RuntimeError("MixLink login agreement checkbox not found")
        try:
            toggle = button.get_toggle_state()
            if toggle:
                return
        except Exception:
            # Qt may expose the agreement row as a clickable label without a
            # readable TogglePattern. This QPushButton ignores UIA Invoke;
            # use a physical click so the client toggles its checked state.
            pass
        button.click_input()

    @staticmethod
    def _find_login_agreement_control(login: Any) -> Any | None:
        for control in login.descendants():
            try:
                auto_id = str(control.element_info.automation_id or "").casefold()
                text = str(control.window_text() or "")
                if (
                    "surechkbx" in auto_id
                    or "agreement" in auto_id
                    or "阅读并同意" in text
                    or "同意用户协议" in text
                ):
                    return control
            except Exception:
                continue
        return None

    def _main_home_is_visible(self) -> bool:
        try:
            self._connect_main_window(1, required=False)
            return self.main_window is not None and "加入会议" in "\n".join(self._texts(self.main_window))
        except Exception:
            return False

    def _find_meeting_window(self) -> Any | None:
        candidates: list[tuple[int, Any]] = []
        for window in self._process_windows(visible_only=True):
            handle = self._window_handle(window)
            if not handle:
                continue
            title, class_name, executable = self._window_identity(handle)
            if executable.casefold() == "ezmeeting.exe":
                text = "\n".join(self._texts(window))
                # Active meetings use the native title “视频会议” on this
                # client build.  RoomWidget is retained for builds that expose
                # that UIA wrapper, while the visible meeting controls cover
                # title-less variants.  Do not treat arbitrary EZMeeting
                # helper windows as an active meeting.
                if (
                    window.class_name() == "RoomWidget"
                    or title == "视频会议"
                    or any(value in text for value in ("结束会议", "离开会议", "参会成员", "共享"))
                ):
                    candidates.append((3, window))
            elif executable.casefold() == "ezeasylink.exe" and class_name not in {
                self.MAIN_CLASS,
                self.LOGIN_CLASS,
                "videoconference::JoinConfDialog",
            }:
                text = "\n".join(self._texts(window))
                if any(value in text for value in ("结束会议", "离开会议", "参会者", "共享屏幕")):
                    candidates.append((1, window))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def _collapse_side_panels(self, window: Any) -> bool:
        """Close the optional notes/transcription area without touching meeting actions.

        MixLink exposes the right-hand area differently across builds: one close
        button can hide the complete area, or each visible page can have its own
        close button.  Locate a close control only when it is on the same header
        row and to the right of a known panel title.  This deliberately excludes
        the meeting window's own close button and the leave/end controls.
        """
        changed = False
        for _attempt in range(2):
            titles = self._visible_side_panel_titles(window)
            if not titles:
                return changed
            closed = False
            for title in titles:
                control = self._find_side_panel_close_control(window, title)
                if control is None:
                    self._record_side_panel_state(f"side_panel_close_unavailable:{title}")
                    continue
                try:
                    self._activate_control(control)
                    self._record_side_panel_state(f"side_panel_close_clicked:{title}")
                    changed = True
                    closed = True
                    time.sleep(0.2)
                    break
                except Exception:
                    self._record_side_panel_state(f"side_panel_close_failed:{title}")
            if not closed:
                return changed
        return changed

    @staticmethod
    def _visible_side_panel_titles(window: Any) -> list[tuple[str, Any]]:
        found: list[tuple[str, Any]] = []
        for control in MixLinkProvider._visible_controls(window):
            text = str(control.window_text() or "").strip()
            if text in {"语音转写", "会议纪要"}:
                found.append((text, control))
        return found

    @staticmethod
    def _find_side_panel_close_control(window: Any, title: tuple[str, Any]) -> Any | None:
        _title_text, title_control = title
        title_rect = MixLinkProvider._control_rectangle(title_control)
        if title_rect is None:
            return None
        for control in MixLinkProvider._visible_controls(window):
            if control is title_control or not MixLinkProvider._is_safe_close_control(control):
                continue
            rect = MixLinkProvider._control_rectangle(control)
            if rect is None:
                continue
            # The panel close button is a compact icon on the title's header,
            # immediately to its right.  The limits also rule out the main
            # window close button and actions in the bottom meeting toolbar.
            if (
                rect.left < title_rect.right
                or rect.left - title_rect.right > 360
                or rect.width() > 80
                or rect.height() > 80
                or rect.bottom < title_rect.top - 24
                or rect.top > title_rect.bottom + 24
            ):
                continue
            return control
        return None

    @staticmethod
    def _is_safe_close_control(control: Any) -> bool:
        try:
            text = str(control.window_text() or "").strip()
            auto_id = str(control.element_info.automation_id or "").casefold()
            if any(value in text for value in ("离开会议", "结束会议", "退出会议")):
                return False
            return text in {"", "×", "✕", "关闭"} or "close" in auto_id
        except Exception:
            return False

    @staticmethod
    def _visible_controls(window: Any) -> list[Any]:
        controls = []
        try:
            controls = list(window.descendants())
        except Exception:
            return []
        visible = []
        for control in controls:
            try:
                if control.is_visible():
                    visible.append(control)
            except Exception:
                # Some Qt UIA elements do not expose IsOffscreen.  Retain
                # them only when they can still be bounded safely below.
                if MixLinkProvider._control_rectangle(control) is not None:
                    visible.append(control)
        return visible

    @staticmethod
    def _control_rectangle(control: Any) -> Any | None:
        try:
            rect = control.rectangle()
            if rect.width() <= 0 or rect.height() <= 0:
                return None
            return rect
        except Exception:
            return None

    def _process_windows(self, visible_only: bool) -> list[Any]:
        names = {name.casefold() for name in self.PROCESS_NAMES}
        results = []
        for window in self._uia_windows(visible_only):
            try:
                handle = int(window.handle)
                _title, _class, executable = self._window_identity(handle)
                if executable.casefold() in names and (not visible_only or window.is_visible()):
                    results.append(window)
            except Exception:
                continue
        return results

    def _find_window_by_class(self, class_name: str) -> Any | None:
        # MixLink's Qt main window has a stable UIA class. Prefer this over a
        # process query, which can transiently fail while CEF/Qt is starting.
        for window in self._uia_windows(visible_only=True):
            try:
                if window.class_name() == class_name:
                    return window
            except Exception:
                continue
        for window in self._process_windows(visible_only=True):
            try:
                if window.class_name() == class_name:
                    return window
            except Exception:
                continue
        return None

    @staticmethod
    def _uia_windows(visible_only: bool) -> list[Any]:
        from pywinauto import Desktop  # type: ignore

        results = []
        for window in Desktop(backend="uia").windows():
            try:
                if not visible_only or window.is_visible():
                    results.append(window)
            except Exception:
                continue
        return results

    def _record_startup_state(self, phase: str) -> None:
        """Keep concise cold-start evidence for the task diagnostic file."""
        try:
            root = str(self.executable.parent if self.executable else "").casefold()
            processes = sorted(
                f"{process.executable_name} pid={process.pid}"
                for process in _list_processes()
                if str(process.executable_path).casefold().startswith(root)
            )
        except Exception as exc:
            processes = [f"process_query_unavailable={exc}"]
        try:
            windows = sorted(
                f"title={window.window_text()!r} class={window.class_name()!r}"
                for window in self._uia_windows(visible_only=False)
                if window.class_name() in {self.MAIN_CLASS, self.LOGIN_CLASS}
            )
        except Exception as exc:
            windows = [f"window_query_error={exc}"]
        snapshot = f"processes={processes}; windows={windows}"
        if snapshot != self._last_process_snapshot:
            self._startup_observations.append(f"startup {phase}: {snapshot}")
            self._last_process_snapshot = snapshot

    def _record_join_state(self, phase: str) -> None:
        """Record window-level evidence without reading credentials or edit values."""
        try:
            windows = sorted(
                f"title={window.window_text()!r} class={window.class_name()!r}"
                for window in self._uia_windows(visible_only=True)
                if window.class_name()
                in {
                    self.MAIN_CLASS,
                    self.LOGIN_CLASS,
                    "videoconference::JoinConfDialog",
                    "RoomWidget",
                    "Qt5152QWindowIcon",
                }
            )
        except Exception as exc:
            windows = [f"window_query_unavailable={exc}"]
        self._join_observations.append(f"join {phase}: windows={windows}")

    def _record_side_panel_state(self, phase: str) -> None:
        """Keep one concise observation per side-panel outcome per task."""
        if phase in self._side_panel_observations:
            return
        self._side_panel_observations.add(phase)
        self._record_join_state(phase)

    def _wait_for_window_class(self, class_name: str, timeout_seconds: float) -> Any | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            window = self._find_window_by_class(class_name)
            if window is not None:
                return window
            time.sleep(0.25)
        return None

    @staticmethod
    def _find_by_auto_id_suffix(window: Any, suffix: str) -> Any | None:
        for control in window.descendants():
            if str(control.element_info.automation_id or "").endswith(suffix):
                return control
        return None

    @staticmethod
    def _find_text_control(window: Any, value: str) -> Any | None:
        for control in window.descendants():
            if value in str(control.window_text() or ""):
                return control
        return None

    @staticmethod
    def _click_named(window: Any, names: list[str]) -> bool:
        for control in window.descendants():
            text = str(control.window_text() or "")
            if any(name == text or name in text for name in names):
                try:
                    MixLinkProvider._activate_control(control)
                    return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _click_exact_named(window: Any, names: list[str]) -> bool:
        for control in window.descendants():
            if str(control.window_text() or "") not in names:
                continue
            try:
                MixLinkProvider._activate_control(control)
                return True
            except Exception:
                continue
        return False

    @staticmethod
    def _activate_control(control: Any) -> None:
        """Use Qt's UIA invoke action; click_input alone does not submit JoinConfDialog."""
        try:
            control.click()
            return
        except Exception:
            control.click_input()

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
    def _texts(window: Any) -> list[str]:
        values: list[str] = []
        try:
            values.extend(str(value) for value in window.texts() if str(value))
        except Exception:
            pass
        try:
            values.extend(str(c.window_text()) for c in window.descendants() if str(c.window_text() or ""))
        except Exception:
            pass
        return values

    @staticmethod
    def _set_edit_text(edit: Any, text: str) -> None:
        try:
            edit.set_edit_text("")
            edit.set_edit_text(text)
        except Exception:
            edit.click_input()
            edit.type_keys("^a{BACKSPACE}", set_foreground=False)
            edit.type_keys(text, with_spaces=True, set_foreground=False)

    @staticmethod
    def _window_handle(window: Any) -> int | None:
        try:
            return int(window.handle)
        except Exception:
            return None

    @staticmethod
    def _is_window_visible(handle: int) -> bool:
        try:
            import win32gui  # type: ignore

            return bool(win32gui.IsWindow(handle) and win32gui.IsWindowVisible(handle))
        except Exception:
            return False

    @staticmethod
    def _window_exists(handle: int) -> bool:
        try:
            import win32gui  # type: ignore

            return bool(win32gui.IsWindow(handle))
        except Exception:
            return False

    def _meeting_has_ended_text(self, window: Any) -> bool:
        text = "\n".join(self._texts(window))
        return any(
            marker in text
            for marker in ("会议已结束", "会议已被结束", "会议已关闭", "会议不存在")
        )

    def _has_finished_dialog(self) -> bool:
        try:
            windows = self._uia_windows(visible_only=True)
            if self.main_window is not None:
                windows.append(self.main_window)
            for window in windows:
                if self._has_visible_text(window, "请评价本次会议体验"):
                    return True
        except Exception:
            # A transient UIA failure must not stop an active recording.
            return False
        return False

    @staticmethod
    def _has_visible_text(window: Any, value: str) -> bool:
        controls = [window]
        try:
            controls.extend(window.descendants())
        except Exception:
            pass
        for control in controls:
            try:
                if value not in str(control.window_text() or ""):
                    continue
                if control.is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _window_identity(handle: int) -> tuple[str, str, str]:
        import win32gui  # type: ignore
        import win32process  # type: ignore

        _thread, pid = win32process.GetWindowThreadProcessId(handle)
        executable_path = _query_process_path(int(pid))
        executable = executable_path.name if executable_path is not None else ""
        return win32gui.GetWindowText(handle), win32gui.GetClassName(handle), executable

    @staticmethod
    def _bring_to_front(window: Any) -> None:
        try:
            window.set_focus()
        except Exception:
            pass

    @staticmethod
    def _ensure_pywinauto() -> None:
        try:
            import pywinauto  # noqa: F401
        except ModuleNotFoundError as exc:
            raise RuntimeError("pywinauto is not installed; run pip install -r requirements.txt") from exc
