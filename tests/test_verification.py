"""验证码服务：哈希、TTL、尝试次数与限流。"""

from __future__ import annotations

import pytest

from app.db import connect, init_schema
from app.settings import VerificationConfig
from app.sms.base import SmsSendError, SmsSendResult, SmsProvider
from app.sms.mock import MockSmsProvider
from app.sms.verification import (
    SmsCodeAttemptsExceeded,
    SmsCodeExpired,
    SmsRateLimited,
    SmsVerificationService,
    hash_code,
)


def _service(**overrides):
    conn = connect(":memory:")
    init_schema(conn)
    config = VerificationConfig(
        code_pepper="test-pepper",
        **overrides,
    )
    provider = MockSmsProvider()
    clock = [1_700_000_000.0]
    service = SmsVerificationService(conn, provider, config, clock=lambda: clock[0])
    return conn, provider, clock, service


def test_send_stores_hash_not_plaintext():
    conn, provider, _clock, service = _service()
    result = service.send_code("13800000000", ip="1.1.1.1")

    assert result.phone == "+8613800000000"
    assert result.cooldown_seconds == 60
    assert len(provider.sent) == 1
    phone, code = provider.sent[0]
    assert phone == "+8613800000000"
    assert len(code) == 6 and code.isdigit()

    row = conn.execute("SELECT phone, code_hash, attempts FROM sms_codes").fetchone()
    assert row["phone"] == "+8613800000000"
    assert row["code_hash"] != code
    assert row["code_hash"] == hash_code(code, "test-pepper")
    assert row["attempts"] == 0


def test_verify_success_is_single_use():
    conn, provider, _clock, service = _service()
    service.send_code("13800000000")
    code = provider.sent[0][1]

    assert service.verify_code("13800000000", code) is True
    with pytest.raises(SmsCodeExpired):
        service.verify_code("13800000000", code)
    assert conn.execute("SELECT COUNT(*) FROM sms_codes").fetchone()[0] == 0


def test_wrong_code_counts_attempts():
    conn, provider, _clock, service = _service()
    service.send_code("13800000000")
    real_code = provider.sent[0][1]
    wrong = "000000" if real_code != "000000" else "111111"

    for _ in range(4):
        assert service.verify_code("13800000000", wrong) is False
    assert service.verify_code("13800000000", wrong) is False  # 第 5 次
    with pytest.raises(SmsCodeAttemptsExceeded):
        service.verify_code("13800000000", wrong)
    assert conn.execute("SELECT COUNT(*) FROM sms_codes").fetchone()[0] == 0


def test_expired_code_rejected():
    conn, provider, clock, service = _service()
    service.send_code("13800000000")
    code = provider.sent[0][1]
    clock[0] += 301

    with pytest.raises(SmsCodeExpired):
        service.verify_code("13800000000", code)
    assert conn.execute("SELECT COUNT(*) FROM sms_codes").fetchone()[0] == 0


def test_cooldown_blocks_immediate_resend():
    conn, provider, clock, service = _service()
    service.send_code("13800000000")
    with pytest.raises(SmsRateLimited) as exc:
        service.send_code("13800000000")
    assert 0 < exc.value.retry_after <= 60

    clock[0] += 61
    service.send_code("13800000000")
    assert len(provider.sent) == 2


def test_hourly_limit():
    _conn, provider, clock, service = _service(per_hour_limit=2)
    service.send_code("13800000000")
    clock[0] += 61
    service.send_code("13800000000")
    clock[0] += 61
    with pytest.raises(SmsRateLimited):
        service.send_code("13800000000")


def test_ip_hourly_limit():
    _conn, provider, clock, service = _service(ip_hour_limit=1)
    service.send_code("13800000000", ip="1.1.1.1")
    with pytest.raises(SmsRateLimited):
        service.send_code("13900000000", ip="1.1.1.1")


def test_service_usable_across_threads():
    from concurrent.futures import ThreadPoolExecutor

    _conn, provider, _clock, service = _service(per_hour_limit=10, ip_hour_limit=10)

    def send(index: int) -> None:
        service.send_code(f"1380000{index:04d}", ip=f"1.1.1.{index}")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(send, (1, 2)))
    assert len(provider.sent) == 2


def test_provider_failure_cleans_records():
    conn = connect(":memory:")
    init_schema(conn)

    class FailingProvider(SmsProvider):
        name = "failing"

        def send(self, phone: str, code: str) -> SmsSendResult:
            raise SmsSendError("network down", phone=phone)

    service = SmsVerificationService(
        conn,
        FailingProvider(),
        VerificationConfig(code_pepper="pepper"),
        clock=lambda: 1_700_000_000.0,
    )
    with pytest.raises(SmsSendError):
        service.send_code("13800000000")

    assert conn.execute("SELECT COUNT(*) FROM sms_codes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sms_send_events").fetchone()[0] == 0
