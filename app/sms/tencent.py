"""腾讯云短信 SendSms provider。

使用 TC3-HMAC-SHA256 签名，POST JSON 到 sms.tencentcloudapi.com。
参数集与 aiway 的 services/sms_service.py 对齐，但不沿用它的安全边界：
密钥不进仓库、HTTP 200 不等于成功、验证码不写日志。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

import httpx

from app.settings import TencentSmsConfig
from app.sms.base import (
    SmsProvider,
    SmsSendError,
    SmsSendResult,
    normalize_phone,
)

logger = logging.getLogger(__name__)

_TENCENT_SERVICE = "sms"
# 与腾讯云官方 Python SDK 保持一致：只签 content-type 与 host。
_SIGNED_HEADERS = "content-type;host"


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class TencentSmsProvider(SmsProvider):
    name = "tencent"

    def __init__(
        self,
        config: TencentSmsConfig,
        *,
        client: httpx.Client | None = None,
    ):
        self.config = config
        self._client = client
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def _http_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.config.timeout_seconds)
            self._owns_client = True
        return self._client

    def send(self, phone: str, code: str) -> SmsSendResult:
        code = str(code or "").strip()
        if not code:
            raise SmsSendError("验证码为空", phone=str(phone or ""))
        normalized = normalize_phone(phone)

        payload = {
            "PhoneNumberSet": [normalized],
            "SmsSdkAppId": str(self.config.sdk_app_id),
            "SignName": str(self.config.sign_name),
            "TemplateId": str(self.config.template_id),
            "TemplateParamSet": [code],
            "SessionContext": self.config.session_context,
        }
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        timestamp = int(time.time())
        authorization = self._authorization(payload_json, timestamp)

        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json",
            "Host": self.config.endpoint,
            "X-TC-Action": self.config.action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": self.config.version,
            "X-TC-Region": self.config.region,
        }

        response = self._post(payload_json, headers, normalized)
        return self._parse_response(response, normalized)

    # ── HTTP ──────────────────────────────────────────────

    def _post(
        self,
        payload_json: str,
        headers: dict[str, str],
        phone: str,
    ) -> httpx.Response:
        url = f"https://{self.config.endpoint}/"
        client = self._http_client()
        last_exc: Exception | None = None
        for attempt in range(self.config.retries + 1):
            try:
                response = client.post(
                    url,
                    content=payload_json.encode("utf-8"),
                    headers=headers,
                )
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt < self.config.retries:
                    time.sleep(min(2 ** attempt, 4))
                    continue
                raise SmsSendError(
                    f"短信发送网络错误: {exc}", phone=phone
                ) from exc

            if response.status_code >= 500 and attempt < self.config.retries:
                time.sleep(min(2 ** attempt, 4))
                continue
            return response

        # 理论上循环内已抛异常；保留兜底
        raise SmsSendError(f"短信发送失败: {last_exc}", phone=phone)

    # ── 响应 ──────────────────────────────────────────────

    def _parse_response(self, response: httpx.Response, phone: str) -> SmsSendResult:
        if response.status_code != 200:
            body = response.text[:500]
            raise SmsSendError(
                f"短信发送失败，HTTP {response.status_code}: {body}",
                phone=phone,
            )

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise SmsSendError(
                f"短信响应不是合法 JSON: {exc}", phone=phone
            ) from exc

        result = data.get("Response") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            raise SmsSendError("短信响应缺少 Response 字段", phone=phone)

        request_id = str(result.get("RequestId") or "")
        error = result.get("Error")
        if isinstance(error, dict):
            code = str(error.get("Code") or "")
            message = str(error.get("Message") or "unknown error")
            raise SmsSendError(
                f"短信发送失败: {code} {message}",
                code=code,
                phone=phone,
                request_id=request_id,
            )

        statuses = result.get("SendStatusSet")
        if isinstance(statuses, dict):
            statuses = [statuses]
        valid_statuses = [
            item for item in statuses if isinstance(item, dict)
        ] if isinstance(statuses, list) else []
        if not valid_statuses:
            raise SmsSendError(
                "短信响应缺少有效的 SendStatusSet", phone=phone, request_id=request_id
            )

        for item in valid_statuses:
            status_code = str(item.get("Code") or "")
            if status_code != "Ok":
                raise SmsSendError(
                    f"短信发送失败: {status_code} {item.get('Message') or ''}".strip(),
                    code=status_code,
                    phone=str(item.get("PhoneNumber") or phone),
                    request_id=request_id,
                )

        first = valid_statuses[0]
        return SmsSendResult(
            provider=self.name,
            phone=str(first.get("PhoneNumber") or phone),
            request_id=request_id,
            code=str(first.get("Code") or "Ok"),
            message=str(first.get("Message") or ""),
        )

    # ── TC3 签名 ──────────────────────────────────────────

    def _authorization(self, payload_json: str, timestamp: int) -> str:
        date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
        credential_scope = f"{date}/{_TENCENT_SERVICE}/tc3_request"

        canonical_headers = (
            "content-type:application/json\n"
            f"host:{self.config.endpoint}\n"
        )
        canonical_request = (
            "POST\n/\n\n"
            + canonical_headers
            + "\n"
            + _SIGNED_HEADERS
            + "\n"
            + _sha256_hex(payload_json)
        )
        string_to_sign = (
            "TC3-HMAC-SHA256\n"
            f"{timestamp}\n"
            f"{credential_scope}\n"
            f"{_sha256_hex(canonical_request)}"
        )

        secret_date = _hmac_sha256(
            ("TC3" + self.config.secret_key).encode("utf-8"), date
        )
        secret_service = _hmac_sha256(secret_date, _TENCENT_SERVICE)
        secret_signing = _hmac_sha256(secret_service, "tc3_request")
        signature = hmac.new(
            secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        return (
            f"TC3-HMAC-SHA256 Credential={self.config.secret_id}/{credential_scope}, "
            f"SignedHeaders={_SIGNED_HEADERS}, Signature={signature}"
        )
