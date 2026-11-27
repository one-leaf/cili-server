"""短信登录路由：发送、校验、设备令牌与错误码。"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.db import connect, init_schema
from app.main import create_app
from app.services.groups import add_group_member, create_group
from app.services.identity import create_org, create_person, hash_token
from app.settings import Settings, SmsConfig, VerificationConfig
from app.sms.base import SmsProvider, SmsSendError, SmsSendResult
from app.sms.mock import MockSmsProvider


def _settings(
    trust_forwarded_for: bool = False,
    access_log_path: str | None = None,
    **verification,
) -> Settings:
    values = {
        "code_pepper": "test-pepper",
        "cooldown_seconds": 60,
        "per_hour_limit": 5,
        "per_day_limit": 10,
        "ip_hour_limit": 30,
    }
    values.update(verification)
    return Settings(
        sms=SmsConfig(provider="mock"),
        verification=VerificationConfig(**values),
        database_path=":memory:",
        trust_forwarded_for=trust_forwarded_for,
        access_log_path=access_log_path,
    )


@contextmanager
def _client(
    provider=None,
    *,
    phone: str = "13800000000",
    enabled: bool = True,
    trust_forwarded_for: bool = False,
    access_log_path: str | None = None,
    **verification,
):
    conn = connect(":memory:")
    init_schema(conn)
    org = create_org(conn, name="Acme", slug="acme")
    person = create_person(
        conn,
        org_id=str(org["id"]),
        name="张三",
        phone=phone,
        enabled=enabled,
    )
    group = create_group(conn, org_id=str(org["id"]), name="研发部")
    add_group_member(
        conn, group_id=str(group["id"]), person_id=str(person["id"])
    )
    provider = provider or MockSmsProvider()
    app = create_app(
        settings=_settings(
            trust_forwarded_for=trust_forwarded_for,
            access_log_path=access_log_path,
            **verification,
        ),
        conn=conn,
        provider=provider,
    )
    try:
        with TestClient(app) as client:
            yield client, provider, str(org["slug"]), str(person["id"])
    finally:
        conn.close()


def test_health():
    with _client() as (client, _provider, _org_id, _person_id):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["name"] == "cili-server"
        assert body["version"]


def test_public_orgs_listed_by_slug():
    with _client() as (client, _provider, _org_id, _person_id):
        response = client.get("/api/v1/public/orgs")
        assert response.status_code == 200
        assert response.json() == {"orgs": [{"id": "acme", "name": "Acme"}]}


def test_access_log_written_to_file(tmp_path):
    log_path = tmp_path / "access.log"
    with _client(access_log_path=str(log_path)) as (
        client, _provider, _org_id, _person_id
    ):
        assert client.get("/api/v1/health").status_code == 200
    text = log_path.read_text(encoding="utf-8")
    assert "GET /api/v1/health 200" in text


def test_send_and_verify_issues_device_token():
    with _client() as (client, provider, org_id, person_id):
        send = client.post(
            "/api/v1/auth/sms/send",
            json={"org_id": org_id, "phone": "13800000000"},
        )
        assert send.status_code == 200
        assert send.json()["ok"] is True
        assert send.json()["cooldown_seconds"] == 60
        code = provider.sent[0][1]

        verify = client.post(
            "/api/v1/auth/sms/verify",
            json={
                "org_id": org_id,
                "phone": "13800000000",
                "code": code,
                "device_name": "测试笔记本",
            },
        )
        assert verify.status_code == 200
        body = verify.json()
        assert body["token"].startswith("dev_")
        assert body["org"]["id"] == org_id
        assert body["user"]["id"] == person_id
        assert body["user"]["name"] == "张三"
        assert body["user"]["groups"] == ["研发部"]
        assert body["config_version"]

        conn = client.app.state.conn
        row = conn.execute("SELECT token_hash FROM devices").fetchone()
        assert row["token_hash"] == hash_token(body["token"])
        assert row["token_hash"] != body["token"]

        me = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        assert me.status_code == 200
        assert me.json()["user"]["id"] == person_id
        assert me.json()["user"]["groups"] == ["研发部"]

        logout = client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        assert logout.status_code == 200
        after = client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {body['token']}"},
        )
        assert after.status_code == 401
        assert after.json()["error"] == "token_invalid"


def test_unregistered_phone_returns_explicit_error():
    with _client(phone="13900000000") as (client, provider, org_id, _person_id):
        response = client.post(
            "/api/v1/auth/sms/send",
            json={"org_id": org_id, "phone": "13800000000"},
        )
        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "phone_not_registered"
        assert body["detail"] == "找不到用户，请输入正确的手机号"
        assert provider.sent == []


def test_disabled_phone_returns_explicit_error():
    with _client(enabled=False) as (client, provider, org_id, _person_id):
        response = client.post(
            "/api/v1/auth/sms/send",
            json={"org_id": org_id, "phone": "13800000000"},
        )
        assert response.status_code == 403
        body = response.json()
        assert body["error"] == "person_disabled"
        assert body["detail"] == "该用户已被停用，请联系管理员"
        assert provider.sent == []


def test_unknown_org_returns_explicit_error():
    with _client() as (client, provider, _org_id, _person_id):
        response = client.post(
            "/api/v1/auth/sms/send",
            json={"org_id": "org_missing", "phone": "13800000000"},
        )
        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "org_not_found"
        assert body["detail"] == "组织不存在或已下线，请重新连接"
        assert provider.sent == []


def test_rate_limit_maps_to_429():
    with _client() as (client, _provider, org_id, _person_id):
        first = client.post(
            "/api/v1/auth/sms/send",
            json={"org_id": org_id, "phone": "13800000000"},
        )
        assert first.status_code == 200

        second = client.post(
            "/api/v1/auth/sms/send",
            json={"org_id": org_id, "phone": "13800000000"},
        )
        assert second.status_code == 429
        body = second.json()
        assert body["error"] == "rate_limited"
        assert 0 < body["retry_after"] <= 60


def test_invalid_phone_maps_to_400():
    with _client() as (client, provider, org_id, _person_id):
        response = client.post(
            "/api/v1/auth/sms/send",
            json={"org_id": org_id, "phone": "123"},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_phone"
        assert provider.sent == []


def test_wrong_code_maps_to_invalid_code():
    with _client() as (client, provider, org_id, _person_id):
        client.post(
            "/api/v1/auth/sms/send",
            json={"org_id": org_id, "phone": "13800000000"},
        )
        code = provider.sent[0][1]
        wrong = "000000" if code != "000000" else "111111"

        response = client.post(
            "/api/v1/auth/sms/verify",
            json={"org_id": org_id, "phone": "13800000000", "code": wrong},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_code"


def test_forwarded_for_used_only_when_trusted():
    with _client(trust_forwarded_for=True) as (client, _provider, org_id, _person_id):
        response = client.post(
            "/api/v1/auth/sms/send",
            json={"org_id": org_id, "phone": "13800000000"},
            headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.1"},
        )
        assert response.status_code == 200
        row = client.app.state.conn.execute(
            "SELECT ip FROM sms_send_events"
        ).fetchone()
        assert row["ip"] == "1.2.3.4"


def test_provider_failure_maps_to_502():
    class FailingProvider(SmsProvider):
        name = "failing"

        def send(self, phone: str, code: str) -> SmsSendResult:
            raise SmsSendError("upstream down", phone=phone)

    with _client(provider=FailingProvider()) as (client, _provider, org_id, _person_id):
        response = client.post(
            "/api/v1/auth/sms/send",
            json={"org_id": org_id, "phone": "13800000000"},
        )
        assert response.status_code == 502
        assert response.json()["error"] == "upstream_error"
