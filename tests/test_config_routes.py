"""配置下发：组织级共享模型 + 仓库群组对应的工作区、ETag/304。"""

from __future__ import annotations

import time
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.db import connect, init_schema
from app.main import create_app
from app.services.groups import add_group_member, create_group
from app.services.identity import (
    create_device,
    create_org,
    create_person,
    update_org,
    update_person,
)
from app.services.model_config import update_org_model
from app.services.repos import create_repo
from app.settings import Settings, SmsConfig, VerificationConfig
from app.sms.mock import MockSmsProvider


def _settings(git_root: str) -> Settings:
    return Settings(
        sms=SmsConfig(provider="mock"),
        verification=VerificationConfig(code_pepper="test"),
        database_path=":memory:",
        git_root=git_root,
    )


@contextmanager
def _client(tmp_path):
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
    app = create_app(
        settings=_settings(str(tmp_path / "git")),
        conn=conn,
        provider=MockSmsProvider(),
    )
    try:
        with TestClient(app) as client:
            yield client, conn, org, person, group, token, tmp_path
    finally:
        conn.close()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _model() -> dict:
    return {
        "label": "组织模型",
        "protocol": "openai",
        "base_url": "https://llm.example/v1",
        "models": {
            "main": {
                "name": "m1",
                "max_context_tokens": 200000,
                "max_output_tokens": 32768,
                "input": ["text", "image"],
            }
        },
    }


def _setup(conn, org) -> None:
    update_org_model(conn, str(org["id"]), _model())
    update_org(conn, str(org["id"]), web_url="https://cili.example.com")


def test_config_model_and_workspace_from_repo_groups(tmp_path):
    with _client(tmp_path) as (client, conn, org, _person, group, token, _):
        _setup(conn, org)
        create_repo(
            conn,
            git_root=tmp_path / "git",
            org_id=str(org["id"]),
            slug="rd-shared",
            name="研发共享",
            group_id=str(group["id"]),
        )

        response = client.get("/api/v1/config", headers=_auth(token))
        assert response.status_code == 200
        body = response.json()
        assert body["org"]["id"] == "acme"
        assert body["org"]["web_url"] == "https://cili.example.com"
        assert body["user"]["name"] == "张三"
        assert body["user"]["groups"] == ["研发部"]

        model = body["model"]
        assert model["provider_key"] == "org-acme"
        assert model["protocol"] == "openai"
        assert model["models"]["main"]["name"] == "m1"
        assert "api_key" not in model

        workspace = body["workspaces"][0]
        assert workspace["key"] == "rd-shared"
        assert workspace["name"] == "研发共享"
        assert workspace["group_name"] == "研发部"
        assert workspace["memory_enabled"] is True
        git = workspace["git"]
        assert git["remote_url"] == "https://cili.example.com/git/rd-shared.git"
        assert git["branch"] == "main"
        assert git["username"] == "张三"
        assert git["password"] == token
        assert git["sync_mode"] == "remote_wins"

        assert body["version"] == response.headers["etag"].strip('"')
        again = client.get(
            "/api/v1/config",
            headers={**_auth(token), "If-None-Match": response.headers["etag"]},
        )
        assert again.status_code == 304


def test_config_injects_person_model_api_key(tmp_path):
    with _client(tmp_path) as (client, conn, org, person, _group, token, _):
        _setup(conn, org)
        first = client.get("/api/v1/config", headers=_auth(token))
        assert "api_key" not in first.json()["model"]

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


def test_config_requires_model_configuration(tmp_path):
    with _client(tmp_path) as (client, _conn, _org, _person, _group, token, _):
        response = client.get("/api/v1/config", headers=_auth(token))
        assert response.status_code == 409
        assert response.json()["error"] == "org_model_not_configured"


def test_config_only_includes_repos_of_own_groups(tmp_path):
    with _client(tmp_path) as (client, conn, org, person, group, token, _):
        _setup(conn, org)
        other = create_group(conn, org_id=str(org["id"]), name="平台部")
        create_repo(
            conn, git_root=tmp_path / "git", org_id=str(org["id"]),
            slug="mine", name="我的仓库", group_id=str(group["id"]),
        )
        create_repo(
            conn, git_root=tmp_path / "git", org_id=str(org["id"]),
            slug="other", name="别人的仓库", group_id=str(other["id"]),
        )
        add_group_member(
            conn, group_id=str(other["id"]), person_id=str(person["id"])
        )

        body = client.get("/api/v1/config", headers=_auth(token)).json()
        assert [item["key"] for item in body["workspaces"]] == ["mine", "other"]

        conn.execute(
            "DELETE FROM group_members WHERE group_id = ? AND person_id = ?",
            (str(other["id"]), str(person["id"])),
        )
        conn.commit()
        body = client.get("/api/v1/config", headers=_auth(token)).json()
        assert [item["key"] for item in body["workspaces"]] == ["mine"]


def test_config_requires_token_and_enabled_person(tmp_path):
    with _client(tmp_path) as (client, conn, org, person, _group, token, _):
        _setup(conn, org)
        assert client.get("/api/v1/config").status_code == 401

        conn.execute("UPDATE people SET enabled = 0 WHERE id = ?", (person["id"],))
        conn.commit()
        response = client.get("/api/v1/config", headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["error"] == "person_disabled"