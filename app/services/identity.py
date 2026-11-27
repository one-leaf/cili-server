"""组织 / 人员 / 设备令牌。

令牌规则：256 位随机、库中只存 SHA-256 哈希、30 天有效、每次认证滑动续期。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass

from app.sms.base import normalize_phone

DEVICE_TOKEN_TTL_SECONDS = 30 * 24 * 3600
ORG_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class AuthenticatedDevice:
    device_id: str
    org_id: str
    org_name: str
    org_slug: str
    config_version: str
    person_id: str
    person_name: str
    phone: str
    expires_at: int


def hash_token(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def create_org(
    conn: sqlite3.Connection,
    *,
    name: str,
    slug: str,
    now: int | None = None,
) -> sqlite3.Row:
    slug = str(slug or "").strip()
    if not ORG_SLUG_RE.match(slug):
        raise ValueError(
            "组织 slug 只允许小写字母、数字、连字符，且以字母/数字开头"
        )
    org_id = "org_" + uuid.uuid4().hex[:12]
    created = int(now if now is not None else time.time())
    config_version = f"{created}-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "INSERT INTO organizations"
        " (id, name, slug, created_at, config_version)"
        " VALUES (?, ?, ?, ?, ?)",
        (org_id, name, slug, created, config_version),
    )
    conn.commit()
    return get_org(conn, org_id)


def get_org(conn: sqlite3.Connection, org_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM organizations WHERE id = ?", (org_id,)
    ).fetchone()


def get_org_by_ref(conn: sqlite3.Connection, ref: str) -> sqlite3.Row | None:
    """按内部 id 或 slug 查组织；客户端 API 使用 slug 作为 org_id。"""
    value = str(ref or "").strip()
    if not value:
        return None
    return conn.execute(
        "SELECT * FROM organizations WHERE id = ? OR slug = ?",
        (value, value),
    ).fetchone()


def update_org(
    conn: sqlite3.Connection,
    org_id: str,
    *,
    name: str | None = None,
    web_url: str | None = None,
    now: int | None = None,
) -> sqlite3.Row:
    """修改组织名称 / 对外 Web 地址；两者都会随配置下发，所以推进 config_version。"""
    if get_org(conn, org_id) is None:
        raise ValueError("组织不存在")
    fields: list[str] = []
    values: list[object] = []
    if name is not None:
        fields.append("name = ?")
        values.append(str(name))
    if web_url is not None:
        fields.append("web_url = ?")
        values.append(str(web_url))
    if fields:
        values.append(org_id)
        conn.execute(
            f"UPDATE organizations SET {', '.join(fields)} WHERE id = ?", values
        )
        bump_config_version(conn, org_id, now=now)
    return get_org(conn, org_id)


def bump_config_version(
    conn: sqlite3.Connection, org_id: str, *, now: int | None = None
) -> str:
    """组织配置相关变更后推进版本，用于客户端 ETag。"""
    value = f"{int(now if now is not None else time.time())}-{uuid.uuid4().hex[:8]}"
    conn.execute(
        "UPDATE organizations SET config_version = ? WHERE id = ?",
        (value, org_id),
    )
    conn.commit()
    return value


def create_person(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    name: str,
    phone: str,
    email: str = "",
    enabled: bool = True,
    note: str = "",
    model_api_key: str = "",
    now: int | None = None,
) -> sqlite3.Row:
    person_id = "u_" + uuid.uuid4().hex[:12]
    normalized = normalize_phone(phone)
    created = int(now if now is not None else time.time())
    conn.execute(
        "INSERT INTO people"
        " (id, org_id, name, phone, email, enabled, note, model_api_key,"
        "  created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            person_id,
            org_id,
            name,
            normalized,
            str(email or ""),
            1 if enabled else 0,
            note,
            str(model_api_key or ""),
            created,
            created,
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM people WHERE id = ?", (person_id,)
    ).fetchone()


def list_people(conn: sqlite3.Connection, org_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM people WHERE org_id = ? ORDER BY name, id", (org_id,)
        )
    )


def update_person(
    conn: sqlite3.Connection,
    person_id: str,
    *,
    name: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    enabled: bool | None = None,
    note: str | None = None,
    model_api_key: str | None = None,
    now: int | None = None,
) -> sqlite3.Row:
    person = conn.execute(
        "SELECT * FROM people WHERE id = ?", (person_id,)
    ).fetchone()
    if person is None:
        raise ValueError("人员不存在")
    fields: list[str] = []
    values: list[object] = []
    if name is not None:
        fields.append("name = ?")
        values.append(str(name))
    if phone is not None:
        fields.append("phone = ?")
        values.append(normalize_phone(phone))
    if email is not None:
        fields.append("email = ?")
        values.append(str(email))
    if enabled is not None:
        fields.append("enabled = ?")
        values.append(1 if enabled else 0)
    if note is not None:
        fields.append("note = ?")
        values.append(str(note))
    if model_api_key is not None:
        fields.append("model_api_key = ?")
        values.append(str(model_api_key))
    fields.append("updated_at = ?")
    values.append(int(now if now is not None else time.time()))
    values.append(person_id)
    conn.execute(f"UPDATE people SET {', '.join(fields)} WHERE id = ?", values)
    conn.commit()
    return conn.execute(
        "SELECT * FROM people WHERE id = ?", (person_id,)
    ).fetchone()


def delete_person(conn: sqlite3.Connection, person_id: str) -> bool:
    person = conn.execute(
        "SELECT * FROM people WHERE id = ?", (person_id,)
    ).fetchone()
    if person is None:
        return False
    org_id = str(person["org_id"])
    # bundle_bindings.target_id 是多态字段，没有外键可级联。
    conn.execute(
        "DELETE FROM bundle_bindings"
        " WHERE target_type = 'person' AND target_id = ?",
        (person_id,),
    )
    conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
    bump_config_version(conn, org_id)
    return True


def list_devices(conn: sqlite3.Connection, org_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT d.*, p.name AS person_name, p.phone"
            " FROM devices d JOIN people p ON p.id = d.person_id"
            " WHERE d.org_id = ? ORDER BY d.created_at DESC, d.id",
            (org_id,),
        )
    )


def revoke_device(conn: sqlite3.Connection, device_id: str) -> bool:
    cursor = conn.execute(
        "UPDATE devices SET revoked = 1 WHERE id = ? AND revoked = 0",
        (device_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def find_person(
    conn: sqlite3.Connection,
    org_id: str,
    phone: str,
    *,
    enabled_only: bool = True,
) -> sqlite3.Row | None:
    normalized = normalize_phone(phone)
    sql = "SELECT * FROM people WHERE org_id = ? AND phone = ?"
    if enabled_only:
        sql += " AND enabled = 1"
    return conn.execute(sql, (org_id, normalized)).fetchone()


def create_device(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    person_id: str,
    name: str = "",
    now: int | None = None,
    ttl_seconds: int = DEVICE_TOKEN_TTL_SECONDS,
) -> tuple[str, int]:
    """创建并返回一次性可见的 token 与到期时间；库中只存哈希。"""
    raw = "dev_" + secrets.token_urlsafe(32)
    created = int(now if now is not None else time.time())
    expires_at = created + int(ttl_seconds)
    conn.execute(
        "INSERT INTO devices"
        " (id, org_id, person_id, name, token_hash, created_at,"
        "  last_seen_at, expires_at, revoked)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (
            "dev_" + uuid.uuid4().hex[:12],
            org_id,
            person_id,
            name,
            hash_token(raw),
            created,
            created,
            expires_at,
        ),
    )
    conn.commit()
    return raw, expires_at


def authenticate_token(
    conn: sqlite3.Connection,
    token: str,
    *,
    now: int | None = None,
    ttl_seconds: int = DEVICE_TOKEN_TTL_SECONDS,
    require_enabled: bool = True,
) -> AuthenticatedDevice | None:
    """校验设备令牌；成功时滑动续期并更新 last_seen。"""
    if not token:
        return None
    current = int(now if now is not None else time.time())
    enabled_clause = " AND p.enabled = 1" if require_enabled else ""
    row = conn.execute(
        "SELECT d.id AS device_id, d.person_id, d.expires_at,"
        "       o.id AS org_id, o.name AS org_name, o.slug AS org_slug,"
        "       o.config_version, p.name AS person_name, p.phone, p.enabled"
        " FROM devices d"
        " JOIN organizations o ON o.id = d.org_id"
        " JOIN people p ON p.id = d.person_id"
        " WHERE d.token_hash = ? AND d.revoked = 0"
        "   AND d.expires_at > ?" + enabled_clause,
        (hash_token(token), current),
    ).fetchone()
    if row is None:
        return None

    new_expiry = current + int(ttl_seconds)
    conn.execute(
        "UPDATE devices SET last_seen_at = ?, expires_at = ? WHERE id = ?",
        (current, new_expiry, row["device_id"]),
    )
    conn.commit()
    return AuthenticatedDevice(
        device_id=str(row["device_id"]),
        org_id=str(row["org_id"]),
        org_name=str(row["org_name"]),
        org_slug=str(row["org_slug"]),
        config_version=str(row["config_version"]),
        person_id=str(row["person_id"]),
        person_name=str(row["person_name"]),
        phone=str(row["phone"]),
        expires_at=new_expiry,
    )


def revoke_token(conn: sqlite3.Connection, token: str) -> bool:
    if not token:
        return False
    cursor = conn.execute(
        "UPDATE devices SET revoked = 1 WHERE token_hash = ? AND revoked = 0",
        (hash_token(token),),
    )
    conn.commit()
    return cursor.rowcount > 0
