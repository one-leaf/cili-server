"""settings：config.json + 环境变量覆盖 + 密钥校验。"""

from __future__ import annotations

import json

import pytest

from app.settings import load_settings
from app.sms.base import SmsConfigError


def _write(tmp_path, data) -> object:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_mock(tmp_path):
    path = _write(tmp_path, {"sms": {"provider": "mock"}})
    settings = load_settings(path)
    assert settings.sms.provider == "mock"
    assert settings.sms.tencent is None


def test_load_tencent_from_file(tmp_path):
    path = _write(
        tmp_path,
        {
            "sms": {
                "provider": "tencent",
                "tencent": {
                    "secret_id": "id-from-file",
                    "secret_key": "key-from-file",
                    "sdk_app_id": "1400000000",
                    "sign_name": "测试签名",
                    "template_id": "123456",
                    "region": "ap-shanghai",
                    "timeout_seconds": 5,
                    "retries": 2,
                },
            }
        },
    )
    settings = load_settings(path)
    cfg = settings.sms.tencent
    assert cfg is not None
    assert cfg.secret_id == "id-from-file"
    assert cfg.secret_key == "key-from-file"
    assert cfg.region == "ap-shanghai"
    assert cfg.timeout_seconds == 5
    assert cfg.retries == 2


def test_env_overrides_secrets(tmp_path, monkeypatch):
    path = _write(
        tmp_path,
        {
            "sms": {
                "provider": "tencent",
                "tencent": {
                    "secret_id": "file-id",
                    "secret_key": "file-key",
                    "sdk_app_id": "app",
                    "sign_name": "sign",
                    "template_id": "tpl",
                },
            }
        },
    )
    monkeypatch.setenv("CILI_SMS_TENCENT_SECRET_ID", "env-id")
    monkeypatch.setenv("CILI_SMS_TENCENT_SECRET_KEY", "env-key")
    settings = load_settings(path)
    assert settings.sms.tencent.secret_id == "env-id"
    assert settings.sms.tencent.secret_key == "env-key"


def test_tencent_missing_fields_fails(tmp_path):
    path = _write(
        tmp_path,
        {"sms": {"provider": "tencent", "tencent": {"secret_id": "only-id"}}},
    )
    with pytest.raises(SmsConfigError) as exc:
        load_settings(path)
    message = str(exc.value)
    assert "secret_key" in message
    assert "sdk_app_id" in message


def test_unknown_provider_fails(tmp_path):
    path = _write(tmp_path, {"sms": {"provider": "nope"}})
    with pytest.raises(SmsConfigError):
        load_settings(path)


def test_missing_file_fails(tmp_path):
    with pytest.raises(SmsConfigError):
        load_settings(tmp_path / "missing.json")


def test_relative_database_path_is_resolved(tmp_path):
    from pathlib import Path

    path = _write(
        tmp_path,
        {"database": {"path": "data/example.db"}, "sms": {"provider": "mock"}},
    )
    settings = load_settings(path)
    assert Path(settings.database_path).is_absolute()
    assert Path(settings.database_path).parts[-2:] == ("data", "example.db")


def test_trust_forwarded_for_env(tmp_path, monkeypatch):
    path = _write(tmp_path, {"sms": {"provider": "mock"}})
    monkeypatch.setenv("CILI_TRUST_FORWARDED_FOR", "1")
    settings = load_settings(path)
    assert settings.trust_forwarded_for is True


def test_secrets_hidden_from_repr(tmp_path):
    path = _write(
        tmp_path,
        {
            "sms": {
                "provider": "tencent",
                "tencent": {
                    "secret_id": "visible-id",
                    "secret_key": "very-secret-key",
                    "sdk_app_id": "app",
                    "sign_name": "sign",
                    "template_id": "tpl",
                },
            }
        },
    )
    settings = load_settings(path)
    text = repr(settings)
    assert "very-secret-key" not in text
    assert "visible-id" not in text
