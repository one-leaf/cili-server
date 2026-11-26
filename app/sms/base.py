"""短信 provider 的公共契约。"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass


class SmsError(Exception):
    """短信发送/配置错误的基类。"""


class SmsConfigError(SmsError):
    """配置缺失或非法。"""


class SmsSendError(SmsError):
    """服务商拒绝发送或网络失败。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        phone: str = "",
        request_id: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.phone = phone
        self.request_id = request_id


class SmsInvalidPhone(SmsSendError):
    """手机号格式错误（客户端错误，不是上游故障）。"""


@dataclass(frozen=True)
class SmsSendResult:
    """一次发送的成功结果。"""

    provider: str
    phone: str
    request_id: str = ""
    code: str = "Ok"
    message: str = ""


def normalize_phone(phone: str) -> str:
    """把常见写法规范成 E.164（中国大陆 11 位补 +86）。"""
    value = re.sub(r"[\s-]", "", str(phone or ""))
    if value.startswith("+"):
        digits = value[1:]
        if digits.isdigit() and 7 <= len(digits) <= 15:
            return value
        raise SmsInvalidPhone(f"手机号格式不正确: {phone}", phone=str(phone or ""))
    if len(value) == 11 and value.startswith("1") and value.isdigit():
        return "+86" + value
    raise SmsInvalidPhone(f"手机号格式不正确: {phone}", phone=str(phone or ""))


class SmsProvider(ABC):
    """短信服务商接口。"""

    name: str = ""

    @abstractmethod
    def send(self, phone: str, code: str) -> SmsSendResult:
        """向手机号发送验证码。失败时抛 SmsSendError。"""
