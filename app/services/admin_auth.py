"""管理端账号与会话：scrypt 密码、随机会话令牌、12 小时过期。"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass

from app.settings import AdminConfig

logger = logging.getLogger(__name__)

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
ADMIN_SESSION_TTL_SECONDS = 12 * 3600


@dataclass(frozen=True)
class AdminIdentity:
    id: str
    username: str


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("密码不能为空")
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${salt.hex()}${digest.hex()}"
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = str(stored).split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


def count_admins(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM admin_users").fetchone()[0])


def create_admin(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    now: int | None = None,
) -> sqlite3.Row:
    username = str(username or "").strip()
    if not username:
        raise ValueError("用户名不能为空")
    if len(str(password or "")) < 8:
        raise ValueError("密码长度至少 8 位")
    admin_id = "adm_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO admin_users (id, username, password_hash, created_at)"
        " VALUES (?, ?, ?, ?)",
        (
            admin_id,
            username,
            hash_password(password),
            int(now if now is not None else time.time()),
        ),
    )
    conn.commit()
    return conn.execute(
        "SELECT * FROM admin_users WHERE id = ?", (admin_id,)
    ).fetchone()


def seed_initial_admin(conn: sqlite3.Connection, config: AdminConfig) -> None:
    if count_admins(conn) > 0:
        return
    username = str(config.initial_username or "").strip()
    password = str(config.initial_password or "")
    if not username or not password:
        logger.warning(
            "[admin] 尚无管理员；请设置 CILI_ADMIN_USERNAME / CILI_ADMIN_PASSWORD "
            "或在 config.json 的 admin 段提供初始账号"
        )
        return
    try:
        create_admin(conn, username=username, password=password)
    except ValueError as exc:
        logger.error("[admin] 初始管理员创建失败: %s", exc)
        return
    logger.info("[admin] 已创建初始管理员 %s", username)


def authenticate_admin(
    conn: sqlite3.Connection, username: str, password: str
) -> AdminIdentity | None:
    row = conn.execute(
        "SELECT * FROM admin_users WHERE username = ?",
        (str(username or "").strip(),),
    ).fetchone()
    if row is None or not verify_password(password, str(row["password_hash"])):
        return None
    return AdminIdentity(id=str(row["id"]), username=str(row["username"]))


def create_admin_session(
    conn: sqlite3.Connection,
    admin_id: str,
    *,
    ttl_seconds: int = ADMIN_SESSION_TTL_SECONDS,
    now: int | None = None,
) -> tuple[str, int]:
    raw = "adm_" + secrets.token_urlsafe(32)
    created = int(now if now is not None else time.time())
    expires_at = created + max(60, int(ttl_seconds))
    conn.execute(
        "INSERT INTO admin_sessions"
        " (id, admin_id, token_hash, created_at, expires_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            "sess_" + uuid.uuid4().hex[:12],
            admin_id,
            _hash_session(raw),
            created,
            expires_at,
        ),
    )
    conn.commit()
    return raw, expires_at


def authenticate_admin_session(
    conn: sqlite3.Connection,
    token: str,
    *,
    now: int | None = None,
) -> AdminIdentity | None:
    if not token:
        return None
    current = int(now if now is not None else time.time())
    row = conn.execute(
        "SELECT u.id, u.username FROM admin_sessions s"
        " JOIN admin_users u ON u.id = s.admin_id"
        " WHERE s.token_hash = ? AND s.expires_at > ?",
        (_hash_session(token), current),
    ).fetchone()
    if row is None:
        return None
    return AdminIdentity(id=str(row["id"]), username=str(row["username"]))


def revoke_admin_session(conn: sqlite3.Connection, token: str) -> bool:
    if not token:
        return False
    cursor = conn.execute(
        "DELETE FROM admin_sessions WHERE token_hash = ?",
        (_hash_session(token),),
    )
    conn.commit()
    return cursor.rowcount > 0


def _hash_session(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def update_admin_account(
    conn: sqlite3.Connection,
    admin_id: str,
    *,
    current_password: str,
    username: str | None = None,
    new_password: str | None = None,
) -> None:
    """修改管理员账号名/密码；成功后吊销该管理员的全部会话。"""
    row = conn.execute(
        "SELECT * FROM admin_users WHERE id = ?", (admin_id,)
    ).fetchone()
    if row is None or not verify_password(
        current_password, str(row["password_hash"])
    ):
        raise ValueError("当前密码不正确")

    fields: list[str] = []
    values: list[object] = []
    if username is not None:
        value = str(username).strip()
        if not value:
            raise ValueError("账号不能为空")
        if value != str(row["username"]):
            exists = conn.execute(
                "SELECT id FROM admin_users WHERE username = ? AND id != ?",
                (value, admin_id),
            ).fetchone()
            if exists is not None:
                raise ValueError("账号已存在")
            fields.append("username = ?")
            values.append(value)
    if new_password is not None:
        if len(str(new_password)) < 8:
            raise ValueError("新密码至少 8 位")
        fields.append("password_hash = ?")
        values.append(hash_password(new_password))
    if not fields:
        raise ValueError("没有需要修改的内容")

    values.append(admin_id)
    conn.execute(
        f"UPDATE admin_users SET {', '.join(fields)} WHERE id = ?", values
    )
    conn.execute(
        "DELETE FROM admin_sessions WHERE admin_id = ?", (admin_id,)
    )
    conn.commit()
