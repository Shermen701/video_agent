from __future__ import annotations

import ctypes
import os
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
WM_CLOSE = 0x0010


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    executable_name: str
    executable_path: Path


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def shutdown_matching_processes(
    *,
    executable_names: set[str],
    allowed_roots: set[Path] | None = None,
    exact_paths: set[Path] | None = None,
    timeout_seconds: float = 5.0,
) -> list[int]:
    """Gracefully close path-verified processes, then terminate leftovers."""
    names = {name.casefold() for name in executable_names}
    roots = {_normalized(path) for path in (allowed_roots or set())}
    paths = {_normalized(path) for path in (exact_paths or set())}
    matches = _matching_processes(names, roots, paths)
    pids = [process.pid for process in matches]
    if not pids:
        return []
    _post_close_to_windows(set(pids))
    remaining = _wait_for_exit(pids, timeout_seconds)
    for pid in remaining:
        _terminate_process(pid)
    return pids


def wait_for_matching_processes_exit(
    *,
    executable_names: set[str],
    allowed_roots: set[Path] | None = None,
    exact_paths: set[Path] | None = None,
    timeout_seconds: float = 5.0,
) -> bool:
    """Wait until only path-verified matching processes have exited."""
    names = {name.casefold() for name in executable_names}
    roots = {_normalized(path) for path in (allowed_roots or set())}
    paths = {_normalized(path) for path in (exact_paths or set())}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _matching_processes(names, roots, paths):
            return True
        time.sleep(0.1)
    return not _matching_processes(names, roots, paths)


def _matching_processes(
    names: set[str], roots: set[str], paths: set[str]
) -> list[ProcessInfo]:
    return [
        process
        for process in _list_processes()
        if process.executable_name.casefold() in names
        and _path_is_allowed(process.executable_path, roots, paths)
    ]


def _path_is_allowed(path: Path, roots: set[str], exact_paths: set[str]) -> bool:
    normalized = _normalized(path)
    if normalized in exact_paths:
        return True
    return any(_is_within(normalized, root) for root in roots)


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _is_within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _list_processes() -> list[ProcessInfo]:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        return []
    results: list[ProcessInfo] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            pid = int(entry.th32ProcessID)
            path = _query_process_path(pid)
            if path is not None:
                results.append(ProcessInfo(pid, str(entry.szExeFile), path))
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return results


def _query_process_path(pid: int) -> Path | None:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def _post_close_to_windows(pids: set[int]) -> None:
    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd: int, _lparam: int) -> bool:
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if int(process_id.value) in pids:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    user32.EnumWindows(callback, 0)


def _wait_for_exit(pids: list[int], timeout_seconds: float) -> list[int]:
    deadline = time.monotonic() + timeout_seconds
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        remaining = [pid for pid in remaining if _process_is_running(pid)]
        if remaining:
            time.sleep(0.1)
    return remaining


def _process_is_running(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        kernel32.CloseHandle(handle)


def _terminate_process(pid: int) -> None:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)
