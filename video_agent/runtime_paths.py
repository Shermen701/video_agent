from __future__ import annotations

import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from video_agent.config import AppConfig
from video_agent.rsa_crypto import ensure_rsa_key_pair

APP_DIR_NAME = "VideoAgent"


def desktop_runtime_dir() -> Path:
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    return desktop / APP_DIR_NAME


def bundled_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS"))
    return Path(__file__).resolve().parents[1]


def ensure_runtime_files(runtime_dir: Path | None = None) -> Path:
    root = runtime_dir or desktop_runtime_dir()
    config_dir = root / "config"
    recordings_dir = root / "recordings"
    logs_dir = root / "logs"
    config_dir.mkdir(parents=True, exist_ok=True)
    recordings_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    config_path = root / "config.yaml"
    if not config_path.exists():
        shutil.copyfile(_bundled_config_template(), config_path)

    private_key = config_dir / "iectp_rsa_private.pem"
    public_key = config_dir / "iectp_rsa_public.pem"
    _copy_if_missing(_bundled_base_file("config/iectp_rsa_private.pem"), private_key)
    _copy_if_missing(_bundled_base_file("config/iectp_rsa_public.pem"), public_key)
    ensure_rsa_key_pair(private_key, public_key)
    return config_path


def apply_runtime_paths(config: AppConfig, runtime_dir: Path | None = None) -> AppConfig:
    root = runtime_dir or desktop_runtime_dir()
    recordings_dir = root / "recordings"
    private_key = root / "config" / "iectp_rsa_private.pem"
    public_key = root / "config" / "iectp_rsa_public.pem"
    return replace(
        config,
        obs=replace(config.obs, recordings_dir=str(recordings_dir)),
        platform=replace(
            config.platform,
            rsa_private_key_path=str(private_key),
            rsa_public_key_path=str(public_key),
        ),
    )


def _bundled_config_template() -> Path:
    base = bundled_base_dir()
    for name in ("config.yaml", "config.example.yaml"):
        candidate = base / name
        if candidate.exists():
            return candidate
    raise FileNotFoundError("bundled config template not found")


def _bundled_base_file(relative_path: str) -> Path:
    return bundled_base_dir() / relative_path


def _copy_if_missing(source: Path, target: Path) -> None:
    if target.exists() or not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
