"""管理端：登录会话与人员/群组/配置包/设备 CRUD。"""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.db import connect, init_schema
from app.main import create_app
from app.services.admin_auth import create_admin
from app.services.identity import create_device, create_org, create_person
from app.settings import AdminConfig, Settings, SmsConfig, VerificationConfig
from app.sms.mock import MockSmsProvider

PASSWORD = "password123"


def _settings(
    admin: AdminConfig | None = None, git_root: str | None = None
) -> Settings:
    return Settings(
        sms=SmsConfig(provider="mock"),
        verification=VerificationConfig(code_pepper="test"),
        admin=admin or AdminConfig(),
        database_path=":memory:",
        git_root=git_root or "data/workspaces",
    )


@contextmanager
def _client(
    admin: AdminConfig | None = None, *, seed_admin: bool = True,
    git_root: str | None = None,
):
    conn = connect(":memory:")
    init_schema(conn)
    org = create_org(conn, name="Acme", slug="acme")
    if seed_admin:
        create_admin(conn, username="admin", password=PASSWORD)
    app = create_app(
        settings=_settings(admin, git_root), conn=conn, provider=MockSmsProvider()
    )
    try:
        with TestClient(app) as client:
            yield client, conn, org
    finally:
        conn.close()


def _login(client: TestClient) -> None:
    response = client.post(
        "/api/v1/admin/login",
        json={"username": "admin", "password": PASSWORD},
    )
    assert response.status_code == 200
    assert "cili_admin" in client.cookies


def test_admin_page_is_served():
    with _client() as (client, _conn, _org):
        response = client.get("/admin")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "cili-server 管理端" in response.text


def test_login_logout_and_me():
    with _client() as (client, _conn, _org):
        assert client.get("/api/v1/admin/me").status_code == 401
        _login(client)

        me = client.get("/api/v1/admin/me")
        assert me.status_code == 200
        assert me.json()["username"] == "admin"

        logout = client.post("/api/v1/admin/logout")
        assert logout.status_code == 200
        assert client.get("/api/v1/admin/me").status_code == 401


def test_login_wrong_password():
    with _client() as (client, _conn, _org):
        response = client.post(
            "/api/v1/admin/login",
            json={"username": "admin", "password": "wrong-password"},
        )
        assert response.status_code == 401
        assert response.json()["error"] == "invalid_credentials"


def test_initial_admin_seed_from_settings():
    admin = AdminConfig(initial_username="root", initial_password="password123")
    with _client(admin=admin, seed_admin=False) as (client, _conn, _org):
        response = client.post(
            "/api/v1/admin/login",
            json={"username": "root", "password": "password123"},
        )
        assert response.status_code == 200


def test_update_org_name():
    with _client() as (client, conn, org):
        _login(client)
        org_id = org["id"]
        before = conn.execute(
            "SELECT config_version FROM organizations WHERE id = ?", (org_id,)
        ).fetchone()[0]

        response = client.patch(
            f"/api/v1/admin/orgs/{org_id}",
            json={"name": "新名字", "web_url": "https://cili.example.com"},
        )
        assert response.status_code == 200
        assert response.json()["org"]["name"] == "新名字"
        assert response.json()["org"]["web_url"] == "https://cili.example.com"
        listed = client.get("/api/v1/admin/orgs").json()["orgs"][0]
        assert listed["name"] == "新名字"
        assert listed["web_url"] == "https://cili.example.com"

        after = conn.execute(
            "SELECT config_version FROM organizations WHERE id = ?", (org_id,)
        ).fetchone()[0]
        assert after != before

        missing = client.patch("/api/v1/admin/orgs/org_missing", json={"name": "x"})
        assert missing.status_code == 404
        assert missing.json()["error"] == "org_not_found"

        blank = client.patch(f"/api/v1/admin/orgs/{org_id}", json={"name": "   "})
        assert blank.status_code == 400

        bad_url = client.patch(
            f"/api/v1/admin/orgs/{org_id}", json={"web_url": "ftp://example.com"}
        )
        assert bad_url.status_code == 400


def test_people_crud():
    with _client() as (client, _conn, org):
        _login(client)
        org_id = org["id"]

        created = client.post(
            "/api/v1/admin/people",
            json={
                "org_id": org_id,
                "name": "张三",
                "phone": "13800000000",
                "email": "zhangsan@example.com",
                "model_api_key": "sk-test-123",
            },
        )
        assert created.status_code == 200
        person = created.json()["person"]
        person_id = person["id"]
        assert person["email"] == "zhangsan@example.com"
        assert person["model_api_key"] == "sk-test-123"

        listed = client.get(f"/api/v1/admin/people?org_id={org_id}")
        assert [p["id"] for p in listed.json()["people"]] == [person_id]
        assert listed.json()["people"][0]["model_api_key"] == "sk-test-123"

        updated = client.patch(
            f"/api/v1/admin/people/{person_id}",
            json={
                "enabled": False,
                "note": "停用",
                "email": "new@example.com",
                "model_api_key": "",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["person"]["enabled"] is False
        assert updated.json()["person"]["note"] == "停用"
        assert updated.json()["person"]["email"] == "new@example.com"
        assert updated.json()["person"]["model_api_key"] == ""

        deleted = client.delete(f"/api/v1/admin/people/{person_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/v1/admin/people?org_id={org_id}").json()["people"] == []


def test_group_and_bundle_crud():
    with _client() as (client, conn, org):
        _login(client)
        org_id = org["id"]
        person = create_person(
            conn, org_id=org_id, name="张三", phone="13800000000"
        )

        group = client.post(
            "/api/v1/admin/groups", json={"org_id": org_id, "name": "研发部"}
        )
        assert group.status_code == 200
        group_id = group.json()["group"]["id"]

        member = client.post(
            f"/api/v1/admin/groups/{group_id}/members",
            json={"person_id": person["id"]},
        )
        assert member.status_code == 200
        assert [p["id"] for p in client.get(
            f"/api/v1/admin/groups/{group_id}/members"
        ).json()["people"]] == [person["id"]]

        bundle = client.post(
            "/api/v1/admin/bundles",
            json={
                "org_id": org_id,
                "name": "研发共享",
                "model": {"label": "group", "models": {"main": {"name": "m1"}}},
                "workspaces": [{"key": "rd", "name": "研发"}],
            },
        )
        assert bundle.status_code == 200
        bundle_id = bundle.json()["bundle"]["id"]

        bound = client.post(
            f"/api/v1/admin/bundles/{bundle_id}/bind",
            json={"target_type": "group", "target_id": group_id, "priority": 5},
        )
        assert bound.status_code == 200
        binding_id = bound.json()["binding"]["id"]

        bindings = client.get(f"/api/v1/admin/bindings?org_id={org_id}")
        assert [b["id"] for b in bindings.json()["bindings"]] == [binding_id]

        assert client.delete(f"/api/v1/admin/bindings/{binding_id}").status_code == 200
        assert client.delete(f"/api/v1/admin/bundles/{bundle_id}").status_code == 200
        assert client.delete(f"/api/v1/admin/groups/{group_id}").status_code == 200


def test_bundle_model_validation():
    with _client() as (client, _conn, org):
        _login(client)
        org_id = org["id"]
        model = {
            "label": "Acme 模型",
            "protocol": "openai",
            "auth_type": "auto",
            "base_url": "https://llm.example/v1",
            "models": {
                "main": {
                    "name": "qwen3-coder",
                    "max_context_tokens": 200000,
                    "max_output_tokens": 32768,
                    "model_type": "reasoning",
                    "input": ["text", "image"],
                }
            },
        }
        created = client.post(
            "/api/v1/admin/bundles",
            json={"org_id": org_id, "name": "模型包", "model": model},
        )
        assert created.status_code == 200
        saved = created.json()["bundle"]["model"]
        assert saved["models"]["main"]["name"] == "qwen3-coder"
        assert "roles" not in saved

        def create(payload):
            return client.post(
                "/api/v1/admin/bundles",
                json={"org_id": org_id, "name": "错误包", "model": payload},
            )

        assert create({**model, "protocol": "grpc"}).status_code == 400
        with_roles = create({**model, "roles": {"master": "main"}})
        assert with_roles.status_code == 200
        assert "roles" not in with_roles.json()["bundle"]["model"]
        assert create(
            {
                **model,
                "models": {"main": {"name": "x", "input": ["image"]}},
            }
        ).status_code == 400
        assert create({**model, "models": {}}).status_code == 400

        bundle_id = created.json()["bundle"]["id"]
        cleared = client.patch(
            f"/api/v1/admin/bundles/{bundle_id}", json={"model": {}}
        )
        assert cleared.status_code == 200
        assert cleared.json()["bundle"]["model"] is None


def test_audit_records_login_and_person_changes():
    with _client() as (client, _conn, org):
        _login(client)
        org_id = org["id"]
        created = client.post(
            "/api/v1/admin/people",
            json={"org_id": org_id, "name": "张三", "phone": "13800000000"},
        ).json()["person"]
        client.patch(
            f"/api/v1/admin/people/{created['id']}",
            json={"enabled": False},
        )

        entries = client.get(f"/api/v1/admin/audit?org_id={org_id}").json()["entries"]
        actions = [entry["action"] for entry in entries]
        assert "person_create" in actions
        assert "person_update" in actions
        assert all(entry["actor"] == "admin" for entry in entries)

        all_entries = client.get("/api/v1/admin/audit").json()["entries"]
        assert any(entry["action"] == "admin_login" for entry in all_entries)


def test_deleting_group_or_person_cleans_bindings():
    with _client() as (client, conn, org):
        _login(client)
        org_id = org["id"]
        person = create_person(
            conn, org_id=org_id, name="张三", phone="13800000000"
        )
        group = client.post(
            "/api/v1/admin/groups", json={"org_id": org_id, "name": "研发部"}
        ).json()["group"]
        bundle = client.post(
            "/api/v1/admin/bundles", json={"org_id": org_id, "name": "包"}
        ).json()["bundle"]

        client.post(
            f"/api/v1/admin/bundles/{bundle['id']}/bind",
            json={"target_type": "group", "target_id": group["id"]},
        )
        client.post(
            f"/api/v1/admin/bundles/{bundle['id']}/bind",
            json={"target_type": "person", "target_id": person["id"]},
        )
        assert len(client.get(f"/api/v1/admin/bindings?org_id={org_id}").json()["bindings"]) == 2

        assert client.delete(f"/api/v1/admin/groups/{group['id']}").status_code == 200
        bindings = client.get(f"/api/v1/admin/bindings?org_id={org_id}").json()["bindings"]
        assert [b["target_type"] for b in bindings] == ["person"]

        assert client.delete(f"/api/v1/admin/people/{person['id']}").status_code == 200
        assert client.get(f"/api/v1/admin/bindings?org_id={org_id}").json()["bindings"] == []


def test_devices_list_and_revoke():
    with _client() as (client, conn, org):
        _login(client)
        person = create_person(
            conn, org_id=org["id"], name="张三", phone="13800000000"
        )
        create_device(
            conn,
            org_id=str(org["id"]),
            person_id=str(person["id"]),
            name="笔记本",
        )
        devices = client.get(f"/api/v1/admin/devices?org_id={org['id']}")
        assert devices.status_code == 200
        device = devices.json()["devices"][0]
        assert device["person_name"] == "张三"
        assert "token_hash" not in device

        revoked = client.post(f"/api/v1/admin/devices/{device['id']}/revoke")
        assert revoked.status_code == 200
        after = client.get(f"/api/v1/admin/devices?org_id={org['id']}").json()
        assert after["devices"][0]["revoked"] is True


def test_repo_crud_and_derived_groups(tmp_path):
    with _client(git_root=str(tmp_path / "git")) as (client, _conn, org):
        _login(client)
        org_id = org["id"]
        created = client.post(
            "/api/v1/admin/repos",
            json={"org_id": org_id, "slug": "demo", "name": "Demo"},
        )
        assert created.status_code == 200
        repo = created.json()["repo"]
        assert repo["slug"] == "demo"

        listed = client.get(f"/api/v1/admin/repos?org_id={org_id}").json()["repos"]
        assert [r["id"] for r in listed] == [repo["id"]]
        assert listed[0]["groups"] == []

        # 授权来自配置包：工作区 key=仓库 slug 且配置包绑定群组
        group = client.post(
            "/api/v1/admin/groups", json={"org_id": org_id, "name": "研发部"}
        ).json()["group"]
        bundle = client.post(
            "/api/v1/admin/bundles",
            json={
                "org_id": org_id,
                "name": "共享",
                "workspaces": [{"key": "demo", "name": "Demo"}],
            },
        ).json()["bundle"]
        client.post(
            f"/api/v1/admin/bundles/{bundle['id']}/bind",
            json={"target_type": "group", "target_id": group["id"]},
        )

        listed = client.get(f"/api/v1/admin/repos?org_id={org_id}").json()["repos"]
        assert [g["name"] for g in listed[0]["groups"]] == ["研发部"]

        assert client.delete(f"/api/v1/admin/repos/{repo['id']}").status_code == 200


def test_admin_mutation_rejects_cross_origin():
    with _client() as (client, _conn, org):
        _login(client)
        response = client.post(
            "/api/v1/admin/people",
            json={"org_id": org["id"], "name": "张三", "phone": "13800000000"},
            headers={"Origin": "http://evil.example"},
        )
        assert response.status_code == 403
        assert response.json()["error"] == "origin_forbidden"
