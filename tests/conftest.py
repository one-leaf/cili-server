"""测试隔离：清掉外部可能存在的短信环境变量。"""

from __future__ import annotations

import pytest

_ENV_NAMES = (
    "CILI_SERVER_CONFIG",
    "CILI_SMS_PROVIDER",
    "CILI_SMS_TENCENT_SECRET_ID",
    "CILI_SMS_TENCENT_SECRET_KEY",
    "CILI_SMS_TENCENT_SDK_APP_ID",
    "CILI_SMS_TENCENT_SIGN_NAME",
    "CILI_SMS_TENCENT_TEMPLATE_ID",
    "CILI_SMS_TENCENT_REGION",
    "CILI_SMS_CODE_PEPPER",
    "CILI_SERVER_DB",
    "CILI_TRUST_FORWARDED_FOR",
    "CILI_ADMIN_USERNAME",
    "CILI_ADMIN_PASSWORD",
    "CILI_ADMIN_COOKIE_SECURE",
    "CILI_GIT_ROOT",
)


@pytest.fixture(autouse=True)
def _clear_sms_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
