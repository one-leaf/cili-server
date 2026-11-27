"""Git Smart HTTP：Basic 设备令牌、群组 ACL、http-backend。"""

from __future__ import annotations

import base64
import shutil
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_schema
from app.main import create_app
from app.services.groups import add_group_member, create_group
from app.services.identity import create_device, create_org, create_person
from app.services.repos import create_repo
from app.settings import Settings, SmsConfig, VerificationConfig
from app.sms.mock import MockSmsProvider

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git CLI not available"
)


def _basic(token: str) -> dict[str, str]:
    raw = base64.b64encode(f"org:{token}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@contextmanager
def _client(tmp_path):
    conn = connect(":memory:")
    init_schema(conn)
    org = create_org(conn, name="Acme", slug="acme")
    person = create_person(
        conn, org_id=str(org["id"]), name="张三", phone="13800000000"
    )
    group = create_group(conn, org_id=str(org["id"]), name="研发部")
    add_group_member(conn, group_id=str(group["id"]), person_id=str(person["id"]))
    token, _ = create_device(
        conn, org_id=str(org["id"]), person_id=str(person["id"])
    )
    git_root = tmp_path / "git"
    repo = create_repo(
        conn,
        git_root=git_root,
        org_id=str(org["id"]),
        slug="demo",
        name="Demo",
        group_id=str(group["id"]),
    )
    settings = Settings(
        sms=SmsConfig(provider="mock"),
        verification=VerificationConfig(code_pepper="test"),
        git_root=str(git_root),
        database_path=":memory:",
    )
    app = create_app(settings=settings, conn=conn, provider=MockSmsProvider())
    try:
        with TestClient(app) as client:
            yield client, conn, org, person, group, repo, token
    finally:
        conn.close()


def test_info_refs_requires_basic_auth(tmp_path):
    with _client(tmp_path) as (client, _conn, _org, _person, _group, _repo, _token):
        response = client.get("/git/demo.git/info/refs?service=git-upload-pack")
        assert response.status_code == 401
        assert "Basic" in response.headers["www-authenticate"]


def test_info_refs_served_for_group_member(tmp_path):
    with _client(tmp_path) as (client, conn, org, _person, _group, _repo, token):
        response = client.get(
            "/git/demo.git/info/refs?service=git-upload-pack",
            headers=_basic(token),
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(
            "application/x-git-upload-pack-advertisement"
        )
        assert b"refs/heads/main" in response.content

        entry = conn.execute(
            "SELECT action, detail FROM audit_log"
            " WHERE action = 'git_info_refs' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert entry is not None
        assert "status=200" in entry["detail"]


def test_unknown_repo_404(tmp_path):
    with _client(tmp_path) as (client, _conn, _org, _person, _group, _repo, token):
        response = client.get(
            "/git/missing.git/info/refs?service=git-upload-pack",
            headers=_basic(token),
        )
        assert response.status_code == 404


def test_acl_denied_for_non_member(tmp_path):
    with _client(tmp_path) as (client, conn, org, _person, _group, _repo, _token):
        outsider = create_person(
            conn, org_id=str(org["id"]), name="李四", phone="13900000000"
        )
        outsider_token, _ = create_device(
            conn, org_id=str(org["id"]), person_id=str(outsider["id"])
        )
        response = client.get(
            "/git/demo.git/info/refs?service=git-upload-pack",
            headers=_basic(outsider_token),
        )
        assert response.status_code == 403


def test_group_member_has_read_write(tmp_path):
    # 授权统一为读写：info/refs(receive-pack) 与 upload-pack 都放行
    with _client(tmp_path) as (client, _conn, _org, _person, _group, _repo, token):
        discover = client.get(
            "/git/demo.git/info/refs?service=git-receive-pack",
            headers=_basic(token),
        )
        assert discover.status_code == 200

        upload = client.get(
            "/git/demo.git/info/refs?service=git-upload-pack",
            headers=_basic(token),
        )
        assert upload.status_code == 200


def test_repo_without_group_binding_is_denied(tmp_path):
    with _client(tmp_path) as (client, conn, _org, _person, _group, _repo, token):
        conn.execute("UPDATE repos SET group_id = NULL")
        conn.commit()
        response = client.get(
            "/git/demo.git/info/refs?service=git-upload-pack",
            headers=_basic(token),
        )
        assert response.status_code == 403
