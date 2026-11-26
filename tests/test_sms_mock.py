"""mock provider：只记录，不联网。"""

from app.sms.mock import MockSmsProvider


def test_mock_records_send():
    provider = MockSmsProvider()
    result = provider.send("13800000000", "123456")
    assert result.code == "Ok"
    assert result.provider == "mock"
    assert provider.sent == [("13800000000", "123456")]
