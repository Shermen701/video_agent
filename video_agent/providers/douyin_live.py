from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from video_agent.app_discovery import find_chrome_executable
from video_agent.models import CaptureTarget, Credentials, MeetingInfo
from video_agent.providers.base import MeetingProvider


class DouyinLiveProvider(MeetingProvider):
    """Record one public Douyin live room through an isolated Chrome window."""

    provider_name = "douyin_live"
    PROCESS_NAME = "chrome.exe"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.executable: Path | None = None
        self.process: subprocess.Popen[bytes] | None = None
        self.window: Any | None = None
        self.live_url = ""
        self.capture_title = ""
        self._diagnostics: list[str] = []
        self._last_chatroom_status = ""

    def launch(self) -> None:
        self._ensure_pywinauto()
        self.executable = find_chrome_executable(str(self.config.get("executable_path") or ""))
        if self.executable is None:
            raise RuntimeError("Douyin Chrome executable not found")

    def ensure_logged_in(self, credentials: Credentials) -> None:
        # Public live rooms are intentionally opened in an isolated unsigned-in
        # profile.  Never submit task credentials to Douyin.
        return None

    def join(self, meeting: MeetingInfo) -> None:
        if self.executable is None:
            raise RuntimeError("Douyin Chrome executable is unavailable")
        self.live_url = self._live_url(meeting)
        port = int(self.config.get("debugging_port") or 9229)
        profile = self._profile_dir()
        profile.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [
                str(self.executable),
                f"--user-data-dir={profile}",
                f"--remote-debugging-port={port}",
                f"--remote-allow-origins=http://127.0.0.1:{port}",
                "--no-first-run",
                "--no-default-browser-check",
                "--autoplay-policy=no-user-gesture-required",
                f"--app={self.live_url}",
            ],
            cwd=str(self.executable.parent),
        )
        timeout = float(self.config.get("playback_timeout_seconds") or 45)
        self._wait_for_playback(timeout)
        self._set_capture_title()
        self.window = self._wait_for_chrome_window(timeout)
        self._enter_native_fullscreen()
        # Fullscreen can rebuild the player several seconds later.  Do not let
        # any one transient DOM state start or abort recording.
        self._verify_player_ready(timeout)

    def prepare_audio_video(self) -> None:
        return None

    def get_capture_target(self) -> CaptureTarget | None:
        target = self._capture_target()
        if target is None:
            raise RuntimeError("recording_failed: Douyin Chrome window is unavailable")
        return target

    def get_audio_capture_target(self) -> CaptureTarget | None:
        return self.get_capture_target()

    def capture_health_check_seconds(self) -> float:
        value = self.config.get("capture_health_check_seconds", 5)
        return max(0.0, float(value))

    def wait_until_finished(self, deadline: datetime) -> None:
        poll_seconds = float(self.config.get("end_poll_seconds") or 5)
        missing_state_polls = 0
        last_current_time: float | None = None
        stalled_since: float | None = None
        while datetime.now(deadline.tzinfo).astimezone() < deadline:
            self._maintain_capture_title()
            # The page can recreate its sidebar while a live room refreshes.
            # Reclose it when its verified control is available.  A transient
            # non-interactive container must not abort an already-running
            # recording; the start-of-recording check above remains strict.
            self._collapse_chatroom(required=False)
            state = self._page_state()
            if state is None:
                missing_state_polls += 1
                if missing_state_polls >= 3:
                    raise RuntimeError("Douyin live page is no longer reachable")
                time.sleep(poll_seconds)
                continue
            missing_state_polls = 0
            if bool(state.get("blocked")):
                raise RuntimeError("Douyin live requires login or verification")
            if bool(state.get("ended")):
                signal = "media" if state.get("mediaEnded") else "page"
                self._finish_after_live_end(signal)
                return

            current_time = float(state.get("currentTime") or 0)
            if (
                last_current_time is None
                or abs(current_time - last_current_time) > 0.1
            ):
                stalled_since = None
            elif bool(state.get("streamInactive")):
                now = time.monotonic()
                if stalled_since is None:
                    stalled_since = now
                elif now - stalled_since >= 30:
                    self._finish_after_live_end("stream_stalled_30s")
                    return
            else:
                stalled_since = None
            last_current_time = current_time
            time.sleep(poll_seconds)

    def _finish_after_live_end(self, signal: str) -> None:
        delay = float(self.config.get("end_delay_seconds") or 5)
        self._diagnostics.append(f"live_end_signal={signal}")
        self._diagnostics.append(f"live_ended_delay_seconds={delay:g}")
        time.sleep(delay)

    def shutdown_application(self) -> None:
        # Close only the window created for this task.  Do not use a path-wide
        # Chrome process kill because it could terminate the user's browser.
        self._close_debug_browser()
        if self.window is not None:
            try:
                self.window.close()
            except Exception:
                pass
        if self.process is not None:
            try:
                self.process.wait(timeout=float(self.config.get("shutdown_timeout_seconds") or 8))
            except subprocess.TimeoutExpired:
                # The bootstrap process can exit while Chrome continues; leave
                # it alone rather than risking a user's existing Chrome session.
                self._diagnostics.append("chrome_process_still_running_after_window_close")
        self.window = None
        self.process = None

    def capture_diagnostics(self, task_dir: Path) -> Path | None:
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / "douyin-diagnostic.txt"
        lines = ["Douyin live diagnostics", f"url_host={urlparse(self.live_url).hostname or ''}"]
        lines.extend(self._diagnostics)
        state = self._page_state()
        if state is not None:
            lines.append(f"page_state={state}")
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def cleanup(self) -> None:
        self.window = None
        self.process = None
        self.capture_title = ""
        self._last_chatroom_status = ""

    def _live_url(self, meeting: MeetingInfo) -> str:
        value = str(meeting.extra.get("liveUrl") or meeting.meeting_no or "").strip()
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname not in {"live.douyin.com", "www.douyin.com"}:
            raise RuntimeError("Douyin task liveUrl must be an https Douyin live-room URL")
        return value

    def _profile_dir(self) -> Path:
        configured = str(self.config.get("profile_dir") or "").strip()
        if configured:
            return Path(configured)
        return Path.home() / "AppData" / "Local" / "VideoAgent" / "DouyinChromeProfile"

    def _wait_for_playback(self, timeout_seconds: float) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_state: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            self._try_start_playback()
            state = self._page_state()
            if state is not None:
                last_state = state
                if bool(state.get("ended")):
                    raise RuntimeError(
                        "meeting_join_failed: Douyin live has already ended; no recording was created"
                    )
                if bool(state.get("blocked")):
                    raise RuntimeError("Douyin live requires login or verification")
                if bool(state.get("playing")):
                    self._diagnostics.append("playback_started")
                    return
            time.sleep(0.5)
        self._diagnostics.append(f"playback_timeout_state={last_state}")
        raise RuntimeError("Douyin live video did not start playing")

    def _try_start_playback(self) -> None:
        """Ask the browser to start the existing HTML5 player with user gesture."""
        self._evaluate(
            """(async () => {
                const video = document.querySelector('video');
                if (!video) return false;
                try { await video.play(); return !video.paused; } catch (_) { return false; }
            })()""",
            user_gesture=True,
        )

    def _verify_player_ready(self, timeout_seconds: float = 45.0) -> None:
        """Wait for two stable samples of the final recording layout."""
        deadline = time.monotonic() + timeout_seconds
        last_result: Any | None = None
        stable_samples = 0
        while time.monotonic() < deadline:
            self._try_start_playback()
            self._maintain_capture_title()
            self._collapse_chatroom(required=False)
            self._select_high_definition(required=False)
            result = self._evaluate(
                """(async () => {
                const video = document.querySelector('video');
                if (!video || !video.videoWidth) return {ok: false, reason: 'video_not_ready'};
                await video.play().catch(() => {});
                const rect = video.getBoundingClientRect();
                const chatroom = document.querySelector('#chatroom');
                const quality = document.querySelector('.QualitySwitchNewPlugin');
                const visibleQualityLabels = quality
                    ? [...quality.querySelectorAll('div')]
                        .filter(element => element.children.length === 0)
                        .filter(element => {
                            const labelRect = element.getBoundingClientRect();
                            return labelRect.width > 0 && labelRect.height > 0;
                        })
                        .map(element => (element.textContent || '').trim())
                    : [];
                return {
                    ok: true,
                    playing: !video.paused && video.readyState >= 3,
                    x: rect.x,
                    width: rect.width,
                    viewportWidth: window.innerWidth,
                    chatroomWidth: chatroom ? chatroom.getBoundingClientRect().width : 0,
                    highDefinition: visibleQualityLabels.includes('\u9ad8\u6e05')
                };
            })()""",
                user_gesture=True,
            )
            last_result = result
            if isinstance(result, dict) and result.get("ok"):
                x = float(result.get("x") or 0)
                width = float(result.get("width") or 0)
                viewport_width = float(result.get("viewportWidth") or 0)
                ready = (
                    bool(result.get("playing"))
                    and viewport_width > 0
                    and abs(x) <= 2
                    and width >= viewport_width - 2
                    and float(result.get("chatroomWidth") or 0) <= 1
                    and bool(result.get("highDefinition"))
                )
                stable_samples = stable_samples + 1 if ready else 0
            else:
                stable_samples = 0
            if stable_samples >= 2:
                self._diagnostics.append("player_ready_after_layout")
                return
            time.sleep(0.5)
        self._diagnostics.append(f"player_layout_not_ready={last_result}")
        raise RuntimeError("Douyin player did not reach a stable recording layout")

    def _select_high_definition(self, required: bool = True) -> bool:
        """Select the anonymous-account HD option and verify the visible label."""
        result = self._evaluate(
            """(async () => {
                const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
                const root = document.querySelector('.QualitySwitchNewPlugin');
                if (!root) return {status: 'control_unavailable'};

                const visibleLeafTexts = () => [...root.querySelectorAll('div')]
                    .filter(element => element.children.length === 0)
                    .filter(element => {
                        const rect = element.getBoundingClientRect();
                        return rect.width > 0 && rect.height > 0;
                    })
                    .map(element => (element.textContent || '').trim())
                    .filter(Boolean);
                if (visibleLeafTexts().includes('\u9ad8\u6e05')) {
                    return {status: 'already_selected'};
                }

                const exactText = (element, text) =>
                    (element.textContent || '').trim() === text;
                const candidates = [...root.querySelectorAll('div')]
                    .filter(element => exactText(element, '\u9ad8\u6e05'))
                    .filter(element => {
                        const parent = element.parentElement;
                        if (!parent) return false;
                        const siblingTexts = [...parent.children]
                            .map(child => (child.textContent || '').trim());
                        return siblingTexts.includes('\u6807\u6e05')
                            && siblingTexts.includes('\u9ad8\u6e05');
                    });
                if (candidates.length !== 1) {
                    return {status: 'option_ambiguous', count: candidates.length};
                }
                candidates[0].click();
                await wait(600);
                return visibleLeafTexts().includes('\u9ad8\u6e05')
                    ? {status: 'selected'}
                    : {status: 'selection_not_applied'};
            })()""",
            user_gesture=True,
        )
        status = str(result.get("status") or "failed") if isinstance(result, dict) else "failed"
        if status in {"selected", "already_selected"}:
            diagnostic = f"quality_{status}_hd"
            if diagnostic not in self._diagnostics:
                self._diagnostics.append(diagnostic)
            return True
        if required:
            self._diagnostics.append(f"quality_selection_failed={result}")
            raise RuntimeError("Douyin HD quality could not be selected")
        return False

    def _collapse_chatroom(self, required: bool) -> bool:
        """Close only the verified right-hand chatroom collapse control."""
        result = self._evaluate(
            """(() => {
                const panel = document.querySelector('#chatroom');
                if (!panel) return {status: 'already_collapsed'};
                const panelRect = panel.getBoundingClientRect();
                if (panelRect.width <= 1) return {status: 'already_collapsed'};
                const largestVideoWidth = () => Math.max(
                    0,
                    ...[...document.querySelectorAll('video')]
                        .map(video => video.getBoundingClientRect())
                        .filter(rect => rect.width > 0 && rect.height > 0)
                        .map(rect => rect.width)
                );
                const videoBefore = largestVideoWidth();

                let controls = [...panel.querySelectorAll('.chatroom_close')];
                if (controls.length === 0) {
                    controls = [...panel.querySelectorAll('svg')]
                        .filter(svg => {
                            const rect = svg.getBoundingClientRect();
                            const path = svg.querySelector('path')?.getAttribute('d') || '';
                            return rect.width >= 20 && rect.width <= 28
                                && rect.height >= 20 && rect.height <= 28
                                && rect.x >= panelRect.x && rect.x <= panelRect.x + 60
                                && rect.y >= panelRect.y && rect.y <= panelRect.y + 60
                                && path.includes('l-4 4-1.06-1.06');
                        })
                        .map(svg => svg.parentElement)
                        .filter(Boolean);
                }
                if (controls.length !== 1) {
                    return {status: 'ambiguous', count: controls.length};
                }
                const controlRect = controls[0].getBoundingClientRect();
                if (controlRect.width <= 0 || controlRect.height <= 0) {
                    return {status: 'unavailable'};
                }
                controls[0].click();
                return new Promise(resolve => setTimeout(() => {
                    const after = panel.getBoundingClientRect().width;
                    const videoAfter = largestVideoWidth();
                    const expanded = videoBefore <= 1 || videoAfter > videoBefore + 1;
                    resolve({
                        status: after <= 1 && expanded ? 'collapsed' : 'not_collapsed',
                        before: panelRect.width,
                        after,
                        videoBefore,
                        videoAfter
                    });
                }, 700));
            })()""",
            user_gesture=True,
        )
        status = str(result.get("status") or "failed") if isinstance(result, dict) else "failed"
        if status != self._last_chatroom_status:
            if status in {"collapsed", "already_collapsed"}:
                self._diagnostics.append(f"chatroom_{status}")
            else:
                self._diagnostics.append(f"chatroom_collapse_failed={result}")
            self._last_chatroom_status = status
        if status in {"collapsed", "already_collapsed"}:
            return status == "collapsed"
        if required:
            raise RuntimeError("Douyin chatroom could not be safely collapsed")
        return False

    def _set_capture_title(self) -> None:
        port = int(self.config.get("debugging_port") or 9229)
        title = f"VideoAgent-Douyin-{port}"
        self.capture_title = title
        result = self._maintain_capture_title()
        if result != title:
            raise RuntimeError("Douyin recording window title could not be prepared")
        self._diagnostics.append("capture_title_prepared")

    def _maintain_capture_title(self) -> str | None:
        if not self.capture_title:
            return None
        title = json.dumps(self.capture_title)
        result = self._evaluate(
            f"""(() => {{
                const expected = {title};
                const enforce = () => {{
                    if (document.title !== expected) document.title = expected;
                }};
                if (window.__videoAgentCaptureTitle !== expected
                    || !window.__videoAgentTitleObserver) {{
                    window.__videoAgentTitleObserver?.disconnect();
                    const observer = new MutationObserver(enforce);
                    observer.observe(document.head || document.documentElement, {{
                        childList: true,
                        characterData: true,
                        subtree: true
                    }});
                    window.__videoAgentTitleObserver = observer;
                    window.__videoAgentCaptureTitle = expected;
                }}
                enforce();
                return document.title;
            }})()"""
        )
        return str(result) if result is not None else None

    def _page_state(self) -> dict[str, Any] | None:
        result = self._evaluate(
            """(() => {
                const video = document.querySelector('video');
                const text = document.body ? document.body.innerText : '';
                const explicitEnded = ['直播已结束', '直播已下播', '直播间已关闭', '本场直播已结束', '主播已离开']
                    .some(x => text.includes(x));
                const mediaEnded = Boolean(video && video.ended);
                let bufferedAhead = 0;
                if (video && video.buffered && video.buffered.length) {
                    try {
                        bufferedAhead = Math.max(
                            0,
                            video.buffered.end(video.buffered.length - 1) - video.currentTime
                        );
                    } catch (_) {}
                }
                const paused = Boolean(video && video.paused);
                const readyState = video ? video.readyState : 0;
                const networkState = video ? video.networkState : 3;
                return {
                    playing: Boolean(video && !video.paused && video.readyState >= 3 && video.videoWidth > 0),
                    ended: explicitEnded || mediaEnded,
                    explicitEnded,
                    mediaEnded,
                    blocked: ['登录后观看', '登录后即可观看', '请登录', '安全验证', '验证码'].some(x => text.includes(x)),
                    paused,
                    readyState,
                    networkState,
                    currentTime: video ? video.currentTime : 0,
                    bufferedAhead,
                    streamInactive: !video || paused || readyState < 3 || networkState === 3 || bufferedAhead <= 0.25
                };
            })()"""
        )
        if not isinstance(result, dict):
            return None
        return {
            "playing": bool(result.get("playing")),
            "ended": bool(result.get("ended")),
            "explicitEnded": bool(result.get("explicitEnded")),
            "mediaEnded": bool(result.get("mediaEnded")),
            "blocked": bool(result.get("blocked")),
            "paused": bool(result.get("paused")),
            "readyState": int(result.get("readyState") or 0),
            "networkState": int(result.get("networkState") or 0),
            "currentTime": float(result.get("currentTime") or 0),
            "bufferedAhead": float(result.get("bufferedAhead") or 0),
            "streamInactive": bool(result.get("streamInactive")),
        }

    def _capture_target(self) -> CaptureTarget | None:
        if self.window is None:
            try:
                self.window = self._wait_for_chrome_window(3)
            except RuntimeError:
                return None
        try:
            handle = int(self.window.handle)
            import win32gui  # type: ignore

            title = str(win32gui.GetWindowText(handle) or "")
            class_name = str(win32gui.GetClassName(handle) or "")
            if title and class_name:
                return CaptureTarget(title, class_name, self.PROCESS_NAME)
        except Exception:
            return None
        return None

    def _enter_native_fullscreen(self) -> None:
        if self.window is None:
            return
        try:
            self.window.set_focus()
            self.window.type_keys("{F11}")
            self._diagnostics.append("chrome_native_fullscreen_requested")
            time.sleep(0.5)
        except Exception:
            # The CSS player-only layout is still safe if this optional native
            # title-bar removal is unavailable on a Chrome build.
            self._diagnostics.append("chrome_native_fullscreen_unavailable")

    def _wait_for_chrome_window(self, timeout_seconds: float) -> Any:
        from pywinauto import Desktop  # type: ignore

        deadline = time.monotonic() + timeout_seconds
        next_title_refresh = 0.0
        while time.monotonic() < deadline:
            now = time.monotonic()
            expected_title = self.capture_title
            if expected_title and now >= next_title_refresh:
                # Douyin can reset document.title during a late React render.
                # Reassert our task-unique title until the matching native
                # window has been bound.
                self._maintain_capture_title()
                next_title_refresh = now + 1.0
            for handle, title in self._visible_chrome_windows():
                if expected_title and expected_title in title:
                    return Desktop(backend="uia").window(handle=handle)
            time.sleep(0.25)
        titles = [title[:120] for _, title in self._visible_chrome_windows()]
        self._diagnostics.append(f"chrome_window_timeout_titles={titles}")
        raise RuntimeError("Douyin Chrome window not found")

    @staticmethod
    def _visible_chrome_windows() -> list[tuple[int, str]]:
        """Enumerate native Chrome windows without relying on UIA discovery."""
        import win32gui  # type: ignore

        windows: list[tuple[int, str]] = []

        def collect(handle: int, _: object) -> None:
            try:
                if not win32gui.IsWindowVisible(handle):
                    return
                if win32gui.GetClassName(handle) != "Chrome_WidgetWin_1":
                    return
                windows.append((handle, str(win32gui.GetWindowText(handle) or "")))
            except Exception:
                return

        win32gui.EnumWindows(collect, None)
        return windows

    def _close_debug_browser(self) -> None:
        port = int(self.config.get("debugging_port") or 9229)
        target = self._debug_target(port)
        if target is None:
            return
        try:
            import websocket  # type: ignore

            connection = websocket.create_connection(str(target["webSocketDebuggerUrl"]), timeout=2)
            try:
                connection.send(json.dumps({"id": 1, "method": "Browser.close"}))
                connection.recv()
            finally:
                connection.close()
        except Exception:
            self._diagnostics.append("debug_browser_close_failed")

    def _evaluate(self, expression: str, user_gesture: bool = False) -> Any | None:
        port = int(self.config.get("debugging_port") or 9229)
        target = self._debug_target(port)
        if target is None:
            return None
        try:
            import websocket  # type: ignore

            connection = websocket.create_connection(str(target["webSocketDebuggerUrl"]), timeout=3)
            try:
                connection.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": expression,
                                "returnByValue": True,
                                "awaitPromise": True,
                                "userGesture": user_gesture,
                            },
                        }
                    )
                )
                response = json.loads(connection.recv())
            finally:
                connection.close()
            return response.get("result", {}).get("result", {}).get("value")
        except Exception as exc:
            self._diagnostics.append(f"debug_evaluate_unavailable={type(exc).__name__}")
            return None

    def _debug_target(self, port: int) -> dict[str, Any] | None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                targets = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError):
            return None
        for target in targets:
            if str(target.get("type") or "") != "page":
                continue
            url = str(target.get("url") or "")
            if url == self.live_url or url.startswith("https://live.douyin.com/"):
                return dict(target)
        return None

    @staticmethod
    def _ensure_pywinauto() -> None:
        try:
            import pywinauto  # type: ignore # noqa: F401
        except ModuleNotFoundError as exc:
            raise RuntimeError("pywinauto is required for Douyin browser automation") from exc
