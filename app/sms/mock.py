"""本地开发用短信 provider：只记录，不实际发送。"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.sms.base import SmsProvider, SmsSendResult

logger = logging.getLogger(__name__)


@dataclass
class MockSmsProvider(SmsProvider):
    """控制台打印验证码，便于本地联调；生产环境不要使用。"""

    name: str = "mock"
    sent: list[tuple[str, str]] = field(default_factory=list)

    def send(self, phone: str, code: str) -> SmsSendResult:
        self.sent.append((str(phone), str(code)))
        logger.warning("[sms:mock] %s -> %s（仅本地开发）", phone, code)
        return SmsSendResult(provider=self.name, phone=str(phone), code="Ok")
