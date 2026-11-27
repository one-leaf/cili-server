"""按组织解析短信 provider：组织配置优先，未配置时回退全局 Settings。"""

from __future__ import annotations

import json
import sqlite3

from app.services.identity import get_org
from app.settings import Settings, TencentSmsConfig
from app.sms import build_provider
from app.sms.base import SmsConfigError, SmsProvider
from app.sms.mock import MockSmsProvider
from app.sms.tencent import TencentSmsProvider

_REQUIRED_FIELDS = ("secret_id", "secret_key", "sdk_app_id", "sign_name", "template_id")


def build_org_provider(data: dict) -> SmsProvider:
    """按组织 sms_config 构建 provider；字段与 config.json 的 tencent 段一致。"""
    provider = str(data.get("provider") or "tencent").strip().lower()
    if provider == "mock":
        return MockSmsProvider()
    if provider != "tencent":
        raise SmsConfigError(f"不支持的短信 provider: {provider}")
    missing = [name for name in _REQUIRED_FIELDS if not str(data.get(name) or "").strip()]
    if missing:
        raise SmsConfigError("腾讯短信配置缺少: " + ", ".join(missing))
    return TencentSmsProvider(
        TencentSmsConfig(
            secret_id=str(data["secret_id"]).strip(),
            secret_key=str(data["secret_key"]).strip(),
            sdk_app_id=str(
                data.get("sdk_app_id") or data.get("sms_sdk_app_id") or ""
            ).strip(),
            sign_name=str(data["sign_name"]).strip(),
            template_id=str(data["template_id"]).strip(),
            region=str(data.get("region") or "ap-guangzhou").strip(),
        )
    )


class SmsProviderResolver:
    """组织级 provider 缓存；admin 保存配置后 invalidate 即可即时生效。"""

    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self._conn = conn
        self._settings = settings
        self._global: SmsProvider | None = None
        self._orgs: dict[str, SmsProvider] = {}

    def _global_provider(self) -> SmsProvider:
        if self._global is None:
            self._global = build_provider(self._settings)
        return self._global

    def get(self, org_id: str) -> SmsProvider:
        org = get_org(self._conn, org_id) if org_id else None
        raw = str(org["sms_config"] or "") if org is not None else ""
        if not raw:
            return self._global_provider()
        if org_id in self._orgs:
            return self._orgs[org_id]
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            raise SmsConfigError("组织短信配置格式错误") from None
        if not isinstance(data, dict):
            raise SmsConfigError("组织短信配置格式错误")
        if not any(
            str(data.get(field) or "").strip()
            for field in ("provider", "secret_id", "secret_key", "sdk_app_id")
        ):
            # 旧库默认 sms_config='{}'，视为未配置
            return self._global_provider()
        provider = build_org_provider(data)
        self._orgs[org_id] = provider
        return provider

    def invalidate(self, org_id: str) -> None:
        provider = self._orgs.pop(org_id, None)
        if provider is not None and hasattr(provider, "close"):
            provider.close()

    def close(self) -> None:
        for provider in self._orgs.values():
            if hasattr(provider, "close"):
                provider.close()
        self._orgs.clear()
        if self._global is not None and hasattr(self._global, "close"):
            self._global.close()
        self._global = None