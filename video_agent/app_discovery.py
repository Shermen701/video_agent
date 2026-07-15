from __future__ import annotations

import os
import subprocess
from pathlib import Path


OBS_CANDIDATES = [
    Path(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"),
    Path(r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe"),
]
OBS_SHORTCUT_KEYWORDS = ["obs"]

TENCENT_MEETING_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Tencent\WeMeet\wemeetapp.exe"),
    Path(r"C:\Program Files\Tencent\WeMeet\wemeetapp.exe"),
    Path.home() / r"AppData\Local\Tencent\WeMeet\wemeetapp.exe",
    Path.home() / r"AppData\Roaming\Tencent\WeMeet\wemeetapp.exe",
]
TENCENT_MEETING_SHORTCUT_KEYWORDS = ["腾讯会议", "tencent meeting", "wemeet"]

DINGTALK_CANDIDATES = [
    Path(r"C:\Program Files (x86)\DingDing\DingtalkLauncher.exe"),
    Path(r"C:\Program Files\DingDing\DingtalkLauncher.exe"),
    Path(r"C:\Program Files (x86)\DingDing\DingTalk.exe"),
    Path(r"C:\Program Files\DingDing\DingTalk.exe"),
    Path.home() / r"AppData\Local\DingDing\DingtalkLauncher.exe",
    Path.home() / r"AppData\Local\DingDing\DingTalk.exe",
    Path.home() / r"AppData\Roaming\DingDing\DingtalkLauncher.exe",
    Path.home() / r"AppData\Roaming\DingDing\DingTalk.exe",
]
DINGTALK_SHORTCUT_KEYWORDS = ["钉钉", "dingtalk", "dingding"]

MIXLINK_CANDIDATES = [
    Path(r"C:\Program Files\MixLink\EzEasyLink.exe"),
    Path(r"C:\Program Files (x86)\MixLink\EzEasyLink.exe"),
    Path(r"D:\Chint\MixLink\EzEasyLink.exe"),
    Path.home() / r"AppData\Local\MixLink\EzEasyLink.exe",
]
MIXLINK_SHORTCUT_KEYWORDS = ["觅讯", "mixlink", "ezeasylink"]


def find_obs_executable(override: str = "") -> Path | None:
    return _find_executable(override, OBS_CANDIDATES, OBS_SHORTCUT_KEYWORDS)


def find_tencent_meeting_executable(override: str = "") -> Path | None:
    return _find_executable(override, TENCENT_MEETING_CANDIDATES, TENCENT_MEETING_SHORTCUT_KEYWORDS)


def find_dingtalk_executable(override: str = "") -> Path | None:
    return _find_executable(override, DINGTALK_CANDIDATES, DINGTALK_SHORTCUT_KEYWORDS)


def find_mixlink_executable(override: str = "") -> Path | None:
    return _find_executable(override, MIXLINK_CANDIDATES, MIXLINK_SHORTCUT_KEYWORDS)


def _find_executable(override: str, candidates: list[Path], shortcut_keywords: list[str]) -> Path | None:
    if override:
        path = Path(override)
        return path if path.exists() else None
    for candidate in candidates:
        if candidate.exists():
            return candidate
    shortcut_target = _find_from_shortcuts(candidates, shortcut_keywords)
    if shortcut_target is not None:
        return shortcut_target
    return None


def _find_from_shortcuts(candidates: list[Path], shortcut_keywords: list[str]) -> Path | None:
    executable_names = {candidate.name.lower() for candidate in candidates}
    shortcut_roots = [
        Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
        Path(os.environ.get("PROGRAMDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
        Path.home() / "Desktop",
    ]
    for root in shortcut_roots:
        if not root.exists():
            continue
        for shortcut in root.rglob("*.lnk"):
            shortcut_name = shortcut.stem.lower()
            if not any(keyword.lower() in shortcut_name for keyword in shortcut_keywords):
                continue
            target = _resolve_shortcut(shortcut)
            if target and target.name.lower() in executable_names and target.exists():
                return target
    return None


def _resolve_shortcut(shortcut: Path) -> Path | None:
    script = (
        "$shell = New-Object -ComObject WScript.Shell; "
        "$shortcut = $shell.CreateShortcut($args[0]); "
        "Write-Output $shortcut.TargetPath"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script, str(shortcut)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    target = result.stdout.strip().strip('"')
    if not target:
        return None
    return Path(target)
