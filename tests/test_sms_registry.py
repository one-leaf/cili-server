"""短信 provider 解析：组织配置优先，缺省回退全局。"""

from __future__ import annotations

import json

import pytest

from app.db import connect, init_schema
from app.services.identity import create_org
from app.settings import Settings, SmsConfig, VerificationConfig
from app.sms.base import SmsConfigError
from app.sms.mock import MockSmsProvider
from app.sms.registry import SmsProviderResolver
from app.sms.tencent import TencentSmsProvider

_TENCENT = {
    "provider": "tencent",
    "secret_id": "id",
    "secret_key": "key",
    "sdk_app_id": "app",
    "sign_name": "sign",
    "template_id": "tpl",
    "region": "ap-guangzhou",
}


def _settings() -> Settings:
    return Settings(
        sms=SmsConfig(provider="mock"),
        verification=VerificationConfig(code_pepper="test"),
        database_path=":memory:",
    )


def _setup(config: dict | None = None):
    conn = connect(":memory:")
    init_schema(conn)
    org = create_org(conn, name="Acme", slug="acme")
    if config is not None:
        conn.execute(
            "UPDATE organizations SET sms_provider = 'tencent', sms_config = ?"
            " WHERE id = ?",
            (json.dumps(config), str(org["id"])),
        )
        conn.commit()
    return conn, str(org["id"])


def test_resolver_falls_back_to_global_provider():
    conn, org_id = _setup()
    resolver = SmsProviderResolver(conn, _settings())
    assert isinstance(resolver.get(org_id), MockSmsProvider)


def test_resolver_builds_tencent_from_org_config():
    conn, org_id = _setup(_TENCENT)
    resolver = SmsProviderResolver(conn, _settings())
    provider = resolver.get(org_id)
    assert isinstance(provider, TencentSmsProvider)
    resolver.close()


def test_resolver_rejects_incomplete_org_config():
    conn, org_id = _setup({"provider": "tencent", "secret_id": "only"})
    resolver = SmsProviderResolver(conn, _settings())
    with pytest.raises(SmsConfigError):
        resolver.get(org_id)