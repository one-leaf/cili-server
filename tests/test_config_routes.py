"""配置下发：个人/群组包合并、工作区并集、ETag/304。"""

from __future__ import annotations

import time
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.db import connect, init_schema
from app.main import create_app
from app.services.bundles import bind_bundle, create_bundle, update_bundle
from app.services.groups import add_group_member, create_group
from app.services.identity import (
    create_device,
    create_org,
    create_person,
    update_org,
    update_person,
)
from app.settings import Settings, SmsConfig, VerificationConfig
from app.sms.mock import MockSmsProvider


def _settings() -> Settings:
    return Settings(
        sms=SmsConfig(provider="mock"),
        verification=VerificationConfig(code_pepper="test"),
        database_path=":memory:",
    )


@contextmanager
def _client():
    conn = connect(":memory:")
    init_schema(conn)
    org = create_org(conn, name="Acme", slug="acme")
    person = create_person(
        conn,
        org_id=str(org["id"]),
        name="张三",
        phone="13800000000",
    )
    group = create_group(conn, org_id=str(org["id"]), name="研发部")
    add_group_member(conn, group_id=str(group["id"]), person_id=str(person["id"]))
    token, _ = create_device(
        conn,
        org_id=str(org["id"]),
        person_id=str(person["id"]),
        name="测试设备",
    )
    app = create_app(settings=_settings(), conn=conn, provider=MockSmsProvider())
    try:
        with TestClient(app) as client:
            yield client, conn, org, person, group, token
    finally:
        conn.close()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_config_merges_personal_model_and_group_workspaces():
    with _client() as (client, conn, org, person, group, token):
        personal = create_bundle(
            conn,
            org_id=str(org["id"]),
            name="张三个人",
            model={"label": "personal", "models": {"main": {"name": "m1"}}},
            workspaces=[{"key": "personal-only", "name": "个人"}],
            priority=10,
        )
        bind_bundle(
            conn, bundle_id=str(personal["id"]), target_type="person",
            target_id=str(person["id"]), priority=10,
        )
        shared = create_bundle(
            conn,
            org_id=str(org["id"]),
            name="研发共享",
            model={"label": "group", "models": {"main": {"name": "m1"}}},
            workspaces=[{"key": "rd-shared", "name": "研发部共享工作区"}],
            priority=5,
        )
        bind_bundle(
            conn, bundle_id=str(shared["id"]), target_type="group",
            target_id=str(group["id"]), priority=5,
        )
        create_bundle(
            conn, org_id=str(org["id"]), name="默认",
            model={"label": "default", "models": {"main": {"name": "m1"}}}, is_default=True,
        )

        response = client.get("/api/v1/config", headers=_auth(token))
        assert response.status_code == 200
        body = response.json()
        assert body["org"]["id"] == org["slug"]
        assert body["user"]["name"] == "张三"
        assert body["user"]["groups"] == ["研发部"]
        assert body["model"]["label"] == "personal"
        assert [ws["key"] for ws in body["workspaces"]] == ["rd-shared"]
        assert body["workspaces"][0]["group_name"] == "研发部"
        assert "personal-only" not in [ws["key"] for ws in body["workspaces"]]
        assert body["version"] == response.headers["etag"].strip('"')

        again = client.get(
            "/api/v1/config",
            headers={**_auth(token), "If-None-Match": response.headers["etag"]},
        )
        assert again.status_code == 304
        assert again.content == b""


def test_group_workspace_conflict_uses_priority():
    with _client() as (client, conn, org, person, group, token):
        group2 = create_group(conn, org_id=str(org["id"]), name="平台部")
        add_group_member(conn, group_id=str(group2["id"]), person_id=str(person["id"]))

        low = create_bundle(
            conn, org_id=str(org["id"]), name="低",
            workspaces=[{"key": "shared", "name": "低优先级"}],
        )
        bind_bundle(conn, bundle_id=str(low["id"]), target_type="group",
                    target_id=str(group["id"]), priority=1)
        high = create_bundle(
            conn, org_id=str(org["id"]), name="高",
            model={"label": "m", "models": {"main": {"name": "m1"}}},
            workspaces=[{"key": "shared", "name": "高优先级"}],
        )
        bind_bundle(conn, bundle_id=str(high["id"]), target_type="group",
                    target_id=str(group2["id"]), priority=2)

        body = client.get("/api/v1/config", headers=_auth(token)).json()
        assert len(body["workspaces"]) == 1
        assert body["workspaces"][0]["name"] == "高优先级"


def test_default_bundle_provides_model_only():
    with _client() as (client, conn, org, _person, _group, token):
        create_bundle(
            conn, org_id=str(org["id"]), name="默认",
            model={"label": "default", "models": {"main": {"name": "m1"}}},
            workspaces=[{"key": "ignored", "name": "默认工作区"}],
            is_default=True,
        )
        body = client.get("/api/v1/config", headers=_auth(token)).json()
        assert body["model"]["label"] == "default"
        assert body["workspaces"] == []


def test_config_requires_token_and_enabled_person():
    with _client() as (client, conn, org, person, _group, token):
        assert client.get("/api/v1/config").status_code == 401

        conn.execute("UPDATE people SET enabled = 0 WHERE id = ?", (person["id"],))
        conn.commit()
        response = client.get("/api/v1/config", headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["error"] == "person_disabled"


def test_config_includes_org_web_url_and_bumps_version():
    with _client() as (client, conn, org, _person, _group, token):
        create_bundle(
            conn, org_id=str(org["id"]), name="组织模型",
            model={"label": "org", "models": {"main": {"name": "m1"}}},
            is_default=True,
        )
        first = client.get("/api/v1/config", headers=_auth(token))
        assert first.json()["org"]["web_url"] == ""

        update_org(conn, str(org["id"]), web_url="https://cili.example.com")

        second = client.get(
            "/api/v1/config",
            headers={
                **_auth(token),
                "If-None-Match": f'"{first.json()["version"]}"',
            },
        )
        assert second.status_code == 200
        assert second.json()["org"]["web_url"] == "https://cili.example.com"
        assert second.json()["version"] != first.json()["version"]


def test_config_injects_person_model_api_key():
    with _client() as (client, conn, org, person, _group, token):
        create_bundle(
            conn, org_id=str(org["id"]), name="组织模型",
            model={"label": "org", "models": {"main": {"name": "m1"}}}, is_default=True,
        )
        first = client.get("/api/v1/config", headers=_auth(token))
        model = first.json()["model"]
        assert model["label"] == "org"
        assert model["provider_key"] == "org-acme"
        assert "api_key" not in model
        assert "model_api_key" not in first.json()["user"]

        # updated_at 是秒级；用明确的后续时间戳，避免同一秒内版本号不变。
        update_person(
            conn,
            str(person["id"]),
            model_api_key="sk-test-123",
            now=int(time.time()) + 5,
        )

        second = client.get(
            "/api/v1/config",
            headers={
                **_auth(token),
                "If-None-Match": f'"{first.json()["version"]}"',
            },
        )
        assert second.status_code == 200
        assert second.json()["model"]["api_key"] == "sk-test-123"
        assert second.json()["version"] != first.json()["version"]


def test_config_injects_workspace_git_block():
    with _client() as (client, conn, org, _person, group, token):
        update_org(conn, str(org["id"]), web_url="https://cili.example.com")
        bundle = create_bundle(
            conn,
            org_id=str(org["id"]),
            name="共享",
            model={"label": "m", "models": {"main": {"name": "m1"}}},
            workspaces=[
                {"key": "rd-shared", "name": "研发共享", "memory_enabled": True}
            ],
        )
        bind_bundle(
            conn, bundle_id=str(bundle["id"]), target_type="group",
            target_id=str(group["id"]),
        )

        body = client.get("/api/v1/config", headers=_auth(token)).json()
        workspace = body["workspaces"][0]
        assert workspace["key"] == "rd-shared"
        assert workspace["memory_enabled"] is True
        git = workspace["git"]
        assert git["remote_url"] == "https://cili.example.com/git/rd-shared.git"
        assert git["branch"] == "main"
        assert git["username"] == "org"
        assert git["password"] == token
        assert git["sync_mode"] == "remote_wins"


def test_config_delivers_person_email():
    with _client() as (client, conn, org, person, _group, token):
        create_bundle(
            conn, org_id=str(org["id"]), name="组织模型",
            model={"label": "org", "models": {"main": {"name": "m1"}}},
            is_default=True,
        )
        assert client.get("/api/v1/config", headers=_auth(token)).json()["user"]["email"] == ""

        update_person(
            conn,
            str(person["id"]),
            email="zhangsan@example.com",
            now=int(time.time()) + 5,
        )

        body = client.get("/api/v1/config", headers=_auth(token)).json()
        assert body["user"]["email"] == "zhangsan@example.com"


def test_config_requires_model_configuration():
    with _client() as (client, _conn, _org, _person, _group, token):
        response = client.get("/api/v1/config", headers=_auth(token))
        assert response.status_code == 409
        assert response.json()["error"] == "org_model_not_configured"


def test_version_changes_when_bundle_changes():
    with _client() as (client, conn, org, _person, _group, token):
        create_bundle(
            conn, org_id=str(org["id"]), name="基础",
            model={"label": "base", "models": {"main": {"name": "m1"}}},
            is_default=True,
        )
        first = client.get("/api/v1/config", headers=_auth(token))
        version = first.json()["version"]

        bundle = create_bundle(
            conn, org_id=str(org["id"]), name="新包", model={"label": "new", "models": {"main": {"name": "m1"}}}
        )
        update_bundle(conn, str(bundle["id"]), model={"label": "updated", "models": {"main": {"name": "m1"}}})

        second = client.get(
            "/api/v1/config",
            headers={**_auth(token), "If-None-Match": f'"{version}"'},
        )
        assert second.status_code == 200
        assert second.json()["version"] != version
