"""短信 provider 工厂。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.sms.base import SmsConfigError, SmsProvider
from app.sms.mock import MockSmsProvider

if TYPE_CHECKING:
    from app.settings import Settings


def build_provider(settings: "Settings") -> SmsProvider:
    provider = settings.sms.provider
    if provider == "tencent":
        if settings.sms.tencent is None:
            raise SmsConfigError("tencent provider 缺少 tencent 配置")
        from app.sms.tencent import TencentSmsProvider

        return TencentSmsProvider(settings.sms.tencent)
    if provider == "mock":
        return MockSmsProvider()
    raise SmsConfigError(f"短信 provider 尚未实现: {provider}")


__all__ = ["build_provider", "MockSmsProvider"]
