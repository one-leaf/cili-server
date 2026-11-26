"""provider 工厂：按配置构建 tencent / mock。"""

from __future__ import annotations

import json

from app.settings import load_settings
from app.sms import build_provider
from app.sms.mock import MockSmsProvider
from app.sms.tencent import TencentSmsProvider


def _write(tmp_path, payload) -> object:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_build_mock(tmp_path):
    path = _write(tmp_path, {"sms": {"provider": "mock"}})
    provider = build_provider(load_settings(path))
    assert isinstance(provider, MockSmsProvider)


def test_build_tencent(tmp_path):
    path = _write(
        tmp_path,
        {
            "sms": {
                "provider": "tencent",
                "tencent": {
                    "secret_id": "id",
                    "secret_key": "key",
                    "sdk_app_id": "app",
                    "sign_name": "sign",
                    "template_id": "tpl",
                },
            }
        },
    )
    provider = build_provider(load_settings(path))
    assert isinstance(provider, TencentSmsProvider)
    provider.close()
