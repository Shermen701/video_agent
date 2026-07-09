from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str = "recorder-001"
    poll_interval_seconds: int = 15
    prepare_before_minutes: int = 5
    max_retries: int = 3


@dataclass(frozen=True)
class PlatformConfig:
    base_url: str = "http://127.0.0.1:8000"
    api_token: str = ""
    app_id: str = ""
    app_secret: str = ""
    tenant_id: int = 1
    token_path: str = "/api/iectp/api/open/token"
    pending_tasks_path: str = "/api/iectp/externalRecord/pendingTasks"
    start_callback_path: str = "/api/iectp/externalRecord/startCallback"
    complete_callback_path: str = "/api/iectp/externalRecord/completeCallback"
    headers: dict[str, str] = field(default_factory=dict)
    provider_aliases: dict[str, str] = field(default_factory=dict)
    rsa_private_key_path: str = "config/iectp_rsa_private.pem"
    rsa_public_key_path: str = "config/iectp_rsa_public.pem"
    rsa_generate_if_missing: bool = True
    rsa_decrypt_passwords: bool = True
    timeout_seconds: int = 20


@dataclass(frozen=True)
class ObsConfig:
    executable_path: str = ""
    websocket_host: str = "127.0.0.1"
    websocket_port: int = 4455
    websocket_password: str = ""
    startup_timeout_seconds: int = 20
    recordings_dir: str = "recordings"


@dataclass(frozen=True)
class UploadConfig:
    part_size_bytes: int = 5 * 1024 * 1024
    retry_count: int = 3


@dataclass(frozen=True)
class MinioConfig:
    endpoint: str = ""
    access_key: str = ""
    secret_key: str = ""
    bucket_name: str = "xny-iectp"
    secure: bool = False
    object_prefix: str = "external-record"


@dataclass(frozen=True)
class AppConfig:
    agent: AgentConfig = field(default_factory=AgentConfig)
    platform: PlatformConfig = field(default_factory=PlatformConfig)
    obs: ObsConfig = field(default_factory=ObsConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    minio: MinioConfig = field(default_factory=MinioConfig)
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    data = _load_mapping(config_path)
    return AppConfig(
        agent=AgentConfig(**dict(data.get("agent") or {})),
        platform=PlatformConfig(**dict(data.get("platform") or {})),
        obs=ObsConfig(**dict(data.get("obs") or {})),
        upload=UploadConfig(**dict(data.get("upload") or {})),
        minio=MinioConfig(**dict(data.get("minio") or {})),
        providers=dict(data.get("providers") or {}),
    )


def parse_config_arg(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--init-only", action="store_true", help="Create desktop runtime files and exit.")
    parser.add_argument("--once", action="store_true", help="Run one polling cycle and exit.")
    parser.add_argument(
        "--smoke-record-seconds",
        type=int,
        default=None,
        help="Run the claimed task immediately and stop recording after this many seconds.",
    )
    return parser.parse_args(argv)


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return dict(json.loads(text))
    try:
        import yaml  # type: ignore

        return dict(yaml.safe_load(text) or {})
    except ModuleNotFoundError:
        return _parse_simple_yaml(text)


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the limited YAML shape used by config.example.yaml without PyYAML."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        key, sep, value = line.strip().partition(":")
        if not sep:
            continue
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value.strip() == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _coerce_scalar(value.strip())
    return root


def _coerce_scalar(value: str) -> Any:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value
