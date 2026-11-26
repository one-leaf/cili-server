"""腾讯云 SendSms provider：TC3 签名、请求体与失败处理。"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from app.settings import TencentSmsConfig
from app.sms.base import SmsSendError
from app.sms.tencent import TencentSmsProvider, normalize_phone


def _config(**overrides) -> TencentSmsConfig:
    data = {
        "secret_id": "AKIDTEST",
        "secret_key": "SECRETKEY",
        "sdk_app_id": "1400000000",
        "sign_name": "测试签名",
        "template_id": "123456",
        "region": "ap-guangzhou",
    }
    data.update(overrides)
    return TencentSmsConfig(**data)


def _provider(handler, **overrides) -> tuple[TencentSmsProvider, httpx.Client]:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return TencentSmsProvider(_config(**overrides), client=client), client


def test_success_request_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["request"] = request
        return httpx.Response(
            200,
            json={
                "Response": {
                    "RequestId": "req-1",
                    "SendStatusSet": [
                        {
                            "Code": "Ok",
                            "Message": "send success",
                            "PhoneNumber": "+8613800000000",
                        }
                    ],
                }
            },
        )

    provider, client = _provider(handler)
    result = provider.send("13800000000", "123456")

    assert result.code == "Ok"
    assert result.request_id == "req-1"
    assert result.phone == "+8613800000000"

    request = captured["request"]
    assert request.method == "POST"
    assert str(request.url) == "https://sms.tencentcloudapi.com/"
    assert request.headers["x-tc-action"] == "SendSms"
    assert request.headers["x-tc-version"] == "2021-01-11"
    assert request.headers["x-tc-region"] == "ap-guangzhou"
    assert request.headers["host"] == "sms.tencentcloudapi.com"
    assert request.headers["content-type"] == "application/json"
    assert re.search(r"Signature=[0-9a-f]{64}$", request.headers["authorization"])
    assert "Credential=AKIDTEST/" in request.headers["authorization"]
    assert "SignedHeaders=content-type;host" in request.headers["authorization"]

    body = json.loads(request.content)
    assert body["PhoneNumberSet"] == ["+8613800000000"]
    assert body["SmsSdkAppId"] == "1400000000"
    assert body["SignName"] == "测试签名"
    assert body["TemplateId"] == "123456"
    assert body["TemplateParamSet"] == ["123456"]
    assert body["SessionContext"] == "cili-org"
    client.close()


def test_tc3_authorization_matches_reference_vector():
    """固定 timestamp/payload 的签名回归（按腾讯云官方 TC3 算法计算）。"""
    provider = TencentSmsProvider(_config())
    payload = json.dumps(
        {
            "PhoneNumberSet": ["+8613800000000"],
            "SmsSdkAppId": "1400000000",
            "SignName": "测试签名",
            "TemplateId": "123456",
            "TemplateParamSet": ["123456"],
            "SessionContext": "cili-org",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    authorization = provider._authorization(payload, 1551113065)
    assert authorization == (
        "TC3-HMAC-SHA256 Credential=AKIDTEST/2019-02-25/sms/tc3_request, "
        "SignedHeaders=content-type;host, "
        "Signature=92e5ec35672bb45ffa9d05e1de5f65f49eb4874e1726c19417a97090f70a7249"
    )


def test_per_phone_failure_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Response": {
                    "RequestId": "req-2",
                    "SendStatusSet": [
                        {
                            "Code": "LimitExceeded.PhoneNumberDailyLimit",
                            "Message": "daily limit",
                            "PhoneNumber": "+8613800000000",
                        }
                    ],
                }
            },
        )

    provider, client = _provider(handler)
    with pytest.raises(SmsSendError) as exc:
        provider.send("13800000000", "123456")
    assert exc.value.code == "LimitExceeded.PhoneNumberDailyLimit"
    assert exc.value.request_id == "req-2"
    client.close()


def test_api_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Response": {
                    "RequestId": "req-3",
                    "Error": {"Code": "AuthFailure", "Message": "bad signature"},
                }
            },
        )

    provider, client = _provider(handler)
    with pytest.raises(SmsSendError) as exc:
        provider.send("13800000000", "123456")
    assert exc.value.code == "AuthFailure"
    assert "bad signature" in str(exc.value)
    client.close()


def test_missing_status_set_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Response": {"RequestId": "req-4"}})

    provider, client = _provider(handler)
    with pytest.raises(SmsSendError):
        provider.send("13800000000", "123456")
    client.close()


def test_invalid_status_set_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"Response": {"RequestId": "req-5", "SendStatusSet": [None]}},
        )

    provider, client = _provider(handler)
    with pytest.raises(SmsSendError):
        provider.send("13800000000", "123456")
    client.close()


def test_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    provider, client = _provider(handler)
    with pytest.raises(SmsSendError) as exc:
        provider.send("13800000000", "123456")
    assert "500" in str(exc.value)
    client.close()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("13800000000", "+8613800000000"),
        ("+8613800000000", "+8613800000000"),
        ("138 0000 0000", "+8613800000000"),
        ("+1 415 555 0100", "+14155550100"),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "12345", "abc", "23800000000"])
def test_normalize_phone_rejects(raw):
    with pytest.raises(SmsSendError):
        normalize_phone(raw)
