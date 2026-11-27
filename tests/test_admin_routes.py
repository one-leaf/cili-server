"""管理端：登录会话与人员/群组/模型设置/仓库/设备 CRUD。"""

from __future__ import annotations

import json
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


def test_sms_settings_masked_and_clear():
    with _client() as (client, conn, org):
        _login(client)
        org_id = org["id"]
        initial = client.get(f"/api/v1/admin/orgs/{org_id}/sms")
        assert initial.status_code == 200
        sms = initial.json()["sms"]
        assert sms["configured"] is False
        assert sms["tencent"]["has_secret_key"] is False

        put = client.put(
            f"/api/v1/admin/orgs/{org_id}/sms",
            json={
                "secret_id": "AKIDexample12345678",
                "secret_key": "super-secret-key-value",
                "sdk_app_id": "1400000000",
                "sign_name": "测试签名",
                "template_id": "123456",
                "region": "ap-guangzhou",
            },
        )
        assert put.status_code == 200
        sms = put.json()["sms"]
        assert sms["configured"] is True
        assert sms["tencent"]["has_secret_key"] is True
        assert sms["tencent"]["secret_id_masked"].startswith("AKID")
        assert sms["tencent"]["sdk_app_id"] == "1400000000"
        assert "super-secret-key-value" not in put.text
        assert "AKIDexample12345678" not in put.text

        # 密钥留空表示不修改
        put2 = client.put(
            f"/api/v1/admin/orgs/{org_id}/sms", json={"template_id": "654321"}
        )
        assert put2.status_code == 200
        assert put2.json()["sms"]["tencent"]["has_secret_key"] is True
        assert put2.json()["sms"]["tencent"]["template_id"] == "654321"

        # 新组织缺密钥 → 400
        other = create_org(conn, name="Other", slug="other")
        missing = client.put(
            f"/api/v1/admin/orgs/{other['id']}/sms",
            json={"sdk_app_id": "1", "sign_name": "s", "template_id": "t"},
        )
        assert missing.status_code == 400

        entries = client.get(f"/api/v1/admin/audit?org_id={org_id}").json()["entries"]
        assert any(entry["action"] == "sms_update" for entry in entries)
        assert "super-secret-key-value" not in json.dumps(entries, ensure_ascii=False)

        cleared = client.delete(f"/api/v1/admin/orgs/{org_id}/sms")
        assert cleared.status_code == 200
        after = client.get(f"/api/v1/admin/orgs/{org_id}/sms").json()["sms"]
        assert after["configured"] is False


def test_backup_and_restore(tmp_path):
    with _client() as (client, _conn, org):
        _login(client)
        org_id = org["id"]
        backup = client.get("/api/v1/admin/backup")
        assert backup.status_code == 200
        assert backup.content.startswith(b"SQLite format 3\x00")

        created = client.post(
            "/api/v1/admin/people",
            json={"org_id": org_id, "name": "临时", "phone": "13700000000"},
        )
        assert created.status_code == 200
        assert len(
            client.get(f"/api/v1/admin/people?org_id={org_id}").json()["people"]
        ) == 1

        path = tmp_path / "backup.db"
        path.write_bytes(backup.content)
        with path.open("rb") as handle:
            restored = client.post(
                "/api/v1/admin/restore",
                files={"file": ("backup.db", handle, "application/octet-stream")},
            )
        assert restored.status_code == 200
        people = client.get(f"/api/v1/admin/people?org_id={org_id}").json()["people"]
        assert people == []

        bad = client.post(
            "/api/v1/admin/restore",
            files={
                "file": ("bad.db", b"not a database", "application/octet-stream")
            },
        )
        assert bad.status_code == 400
        assert bad.json()["error"] == "invalid_backup"


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
                "model_api_key": "sk-test-123",
            },
        )
        assert created.status_code == 200
        person = created.json()["person"]
        person_id = person["id"]
        assert person["model_api_key"] == "sk-test-123"

        listed = client.get(f"/api/v1/admin/people?org_id={org_id}")
        assert [p["id"] for p in listed.json()["people"]] == [person_id]
        assert listed.json()["people"][0]["model_api_key"] == "sk-test-123"

        updated = client.patch(
            f"/api/v1/admin/people/{person_id}",
            json={
                "enabled": False,
                "note": "停用",
                "model_api_key": "",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["person"]["enabled"] is False
        assert updated.json()["person"]["note"] == "停用"
        assert updated.json()["person"]["model_api_key"] == ""

        deleted = client.delete(f"/api/v1/admin/people/{person_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/v1/admin/people?org_id={org_id}").json()["people"] == []


def test_group_crud_with_member(tmp_path):
    with _client(git_root=str(tmp_path / "git")) as (client, conn, org):
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

        assert client.delete(
            f"/api/v1/admin/groups/{group_id}/members/{person['id']}"
        ).status_code == 200
        assert client.delete(f"/api/v1/admin/groups/{group_id}").status_code == 200


def test_model_settings_validation(tmp_path):
    with _client(git_root=str(tmp_path / "git")) as (client, _conn, org):
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
        saved = client.put(
            f"/api/v1/admin/orgs/{org_id}/model", json={"model": model}
        )
        assert saved.status_code == 200
        assert saved.json()["model"]["models"]["main"]["name"] == "qwen3-coder"
        fetched = client.get(f"/api/v1/admin/orgs/{org_id}/model").json()["model"]
        assert fetched["protocol"] == "openai"

        def update(payload):
            return client.put(
                f"/api/v1/admin/orgs/{org_id}/model", json={"model": payload}
            )

        assert update({**model, "protocol": "grpc"}).status_code == 400
        with_roles = update({**model, "roles": {"master": "main"}})
        assert with_roles.status_code == 200
        assert "roles" not in with_roles.json()["model"]
        assert update(
            {**model, "models": {"main": {"name": "x", "input": ["image"]}}}
        ).status_code == 400
        assert update({**model, "models": {}}).status_code == 400

        cleared = update({})
        assert cleared.status_code == 200
        assert cleared.json()["model"] is None


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


def test_deleting_group_removes_repo_binding(tmp_path):
    with _client(git_root=str(tmp_path / "git")) as (client, _conn, org):
        _login(client)
        org_id = org["id"]
        group = client.post(
            "/api/v1/admin/groups", json={"org_id": org_id, "name": "研发部"}
        ).json()["group"]
        repo = client.post(
            "/api/v1/admin/repos",
            json={
                "org_id": org_id,
                "slug": "demo",
                "name": "Demo",
                "group_id": group["id"],
            },
        ).json()["repo"]

        listed = client.get(f"/api/v1/admin/repos?org_id={org_id}").json()["repos"]
        assert listed[0]["group"]["name"] == "研发部"

        assert client.delete(f"/api/v1/admin/groups/{group['id']}").status_code == 200
        listed = client.get(f"/api/v1/admin/repos?org_id={org_id}").json()["repos"]
        assert listed[0]["id"] == repo["id"]
        assert listed[0]["group"] is None


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


def test_repo_groups_api(tmp_path):
    with _client(git_root=str(tmp_path / "git")) as (client, _conn, org):
        _login(client)
        org_id = org["id"]
        group = client.post(
            "/api/v1/admin/groups", json={"org_id": org_id, "name": "研发部"}
        ).json()["group"]
        repo = client.post(
            "/api/v1/admin/repos",
            json={"org_id": org_id, "slug": "demo", "name": "Demo"},
        ).json()["repo"]
        listed = client.get(f"/api/v1/admin/repos?org_id={org_id}").json()["repos"]
        assert listed[0]["group"] is None

        updated = client.put(
            f"/api/v1/admin/repos/{repo['id']}/group",
            json={"group_id": group["id"]},
        )
        assert updated.status_code == 200
        assert updated.json()["repo"]["group"]["name"] == "研发部"

        cleared = client.put(
            f"/api/v1/admin/repos/{repo['id']}/group", json={"group_id": ""}
        )
        assert cleared.status_code == 200
        assert cleared.json()["repo"]["group"] is None

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
