"""配置下发：个人/群组包合并、工作区并集、ETag/304。"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.db import connect, init_schema
from app.main import create_app
from app.services.bundles import bind_bundle, create_bundle, update_bundle
from app.services.groups import add_group_member, create_group
from app.services.identity import create_device, create_org, create_person
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
            model={"label": "personal"},
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
            model={"label": "group"},
            workspaces=[{"key": "rd-shared", "name": "研发部共享工作区"}],
            priority=5,
        )
        bind_bundle(
            conn, bundle_id=str(shared["id"]), target_type="group",
            target_id=str(group["id"]), priority=5,
        )
        create_bundle(
            conn, org_id=str(org["id"]), name="默认",
            model={"label": "default"}, is_default=True,
        )

        response = client.get("/api/v1/config", headers=_auth(token))
        assert response.status_code == 200
        body = response.json()
        assert body["org"]["id"] == org["id"]
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
            model={"label": "default"},
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


def test_version_changes_when_bundle_changes():
    with _client() as (client, conn, org, _person, _group, token):
        first = client.get("/api/v1/config", headers=_auth(token))
        version = first.json()["version"]

        bundle = create_bundle(
            conn, org_id=str(org["id"]), name="新包", model={"label": "new"}
        )
        update_bundle(conn, str(bundle["id"]), model={"label": "updated"})

        second = client.get(
            "/api/v1/config",
            headers={**_auth(token), "If-None-Match": f'"{version}"'},
        )
        assert second.status_code == 200
        assert second.json()["version"] != version
