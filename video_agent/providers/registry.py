from __future__ import annotations

from typing import Any

from video_agent.models import ErrorCode
from video_agent.providers.base import MeetingProvider
from video_agent.providers.dingtalk import DingTalkProvider
from video_agent.providers.mixlink import MixLinkProvider
from video_agent.providers.tencent_meeting import TencentMeetingProvider

_PROVIDERS: dict[str, type[MeetingProvider]] = {
    DingTalkProvider.provider_name: DingTalkProvider,
    MixLinkProvider.provider_name: MixLinkProvider,
    TencentMeetingProvider.provider_name: TencentMeetingProvider,
}


def create_provider(name: str, all_provider_config: dict[str, dict[str, Any]]) -> MeetingProvider:
    provider_cls = _PROVIDERS.get(name)
    if provider_cls is None:
        raise ValueError(f"{ErrorCode.UNKNOWN_PROVIDER.value}: {name}")
    return provider_cls(dict(all_provider_config.get(name) or {}))


def list_providers() -> list[str]:
    return sorted(_PROVIDERS)
