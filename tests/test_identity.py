"""组织 / 人员 / 设备令牌：哈希、滑动续期与吊销。"""

from __future__ import annotations

import pytest

from app.db import connect, init_schema
from app.services.identity import (
    authenticate_token,
    create_device,
    create_org,
    create_person,
    find_person,
    hash_token,
    revoke_token,
)


def test_create_org_rejects_invalid_slug():
    conn = connect(":memory:")
    init_schema(conn)
    for slug in ("Acme", "a/b", "", "-bad", "有中文"):
        with pytest.raises(ValueError):
            create_org(conn, name="X", slug=slug)


def _seed():
    conn = connect(":memory:")
    init_schema(conn)
    org = create_org(conn, name="Acme", slug="acme", now=1_700_000_000)
    person = create_person(
        conn,
        org_id=str(org["id"]),
        name="张三",
        phone="13800000000",
        now=1_700_000_000,
    )
    return conn, org, person


def test_create_and_find_person():
    conn, org, person = _seed()
    found = find_person(conn, str(org["id"]), "138 0000 0000")
    assert found is not None
    assert found["id"] == person["id"]
    assert person["phone"] == "+8613800000000"


def test_device_token_is_hashed_and_slides():
    conn, org, person = _seed()
    token, expires_at = create_device(
        conn,
        org_id=str(org["id"]),
        person_id=str(person["id"]),
        name="笔记本",
        now=1_700_000_000,
    )
    assert token.startswith("dev_")
    row = conn.execute("SELECT token_hash, expires_at FROM devices").fetchone()
    assert row["token_hash"] == hash_token(token)
    assert row["token_hash"] != token
    assert expires_at == 1_700_000_000 + 30 * 24 * 3600

    later = 1_700_000_000 + 3600
    authed = authenticate_token(conn, token, now=later)
    assert authed is not None
    assert authed.org_id == org["id"]
    assert authed.person_id == person["id"]
    assert authed.expires_at == later + 30 * 24 * 3600


def test_expired_or_revoked_token_rejected():
    conn, org, person = _seed()
    token, _expires = create_device(
        conn,
        org_id=str(org["id"]),
        person_id=str(person["id"]),
        now=1_700_000_000,
        ttl_seconds=60,
    )
    assert authenticate_token(conn, token, now=1_700_000_061) is None

    token2, _ = create_device(
        conn,
        org_id=str(org["id"]),
        person_id=str(person["id"]),
        now=1_700_000_000,
    )
    assert revoke_token(conn, token2) is True
    assert authenticate_token(conn, token2, now=1_700_000_001) is None


def test_disabled_person_cannot_authenticate():
    conn, org, person = _seed()
    conn.execute("UPDATE people SET enabled = 0 WHERE id = ?", (person["id"],))
    conn.commit()
    token, _ = create_device(
        conn,
        org_id=str(org["id"]),
        person_id=str(person["id"]),
        now=1_700_000_000,
    )
    assert authenticate_token(conn, token, now=1_700_000_001) is None
