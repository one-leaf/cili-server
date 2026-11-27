"""SQLite 基础设施与短信相关表结构。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    config_version TEXT NOT NULL DEFAULT '',
    web_url TEXT NOT NULL DEFAULT '',
    sms_provider TEXT NOT NULL DEFAULT '',
    sms_config TEXT NOT NULL DEFAULT '{}',
    model_config TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    model_api_key TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE(org_id, phone)
);
CREATE INDEX IF NOT EXISTS idx_people_org_phone
    ON people(org_id, phone);

CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    UNIQUE(org_id, name)
);
CREATE INDEX IF NOT EXISTS idx_groups_org ON groups(org_id);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    PRIMARY KEY(group_id, person_id)
);
CREATE INDEX IF NOT EXISTS idx_group_members_person
    ON group_members(person_id);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    person_id TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_devices_token
    ON devices(token_hash);
CREATE INDEX IF NOT EXISTS idx_devices_person
    ON devices(person_id);

CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    group_id TEXT REFERENCES groups(id) ON DELETE SET NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_repos_org ON repos(org_id);


CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id TEXT NOT NULL DEFAULT '',
    at INTEGER NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_org_at
    ON audit_log(org_id, at DESC, id DESC);

CREATE TABLE IF NOT EXISTS admin_users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS admin_sessions (
    id TEXT PRIMARY KEY,
    admin_id TEXT NOT NULL REFERENCES admin_users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_admin_sessions_token
    ON admin_sessions(token_hash);

CREATE TABLE IF NOT EXISTS sms_codes (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_sms_codes_phone
    ON sms_codes(phone, created_at DESC);

CREATE TABLE IF NOT EXISTS sms_send_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    org_id TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    sent_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sms_send_phone
    ON sms_send_events(phone, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_sms_send_ip
    ON sms_send_events(ip, sent_at DESC);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """打开 SQLite 连接（WAL + Row 工厂）。"""
    target = str(path)
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    # 服务层用 RLock 串行化同一连接的访问；允许跨线程使用，便于 FastAPI
    # 的线程池直接复用服务实例。
    conn = sqlite3.connect(target, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 兼容上一版已经创建的库：补 org_id 列。
    _ensure_column(conn, "organizations", "web_url", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "organizations", "model_config", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(conn, "people", "model_api_key", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "sms_codes", "org_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "sms_send_events", "org_id", "TEXT NOT NULL DEFAULT ''")
    if _ensure_column(
        conn, "repos", "group_id", "TEXT REFERENCES groups(id) ON DELETE SET NULL"
    ):
        # 一次性迁移：旧的仓库-群组多对多表 → 仓库只保留一个群组
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "repo_groups" in tables:
            for row in conn.execute(
                "SELECT repo_id, MIN(group_id) AS group_id FROM repo_groups"
                " GROUP BY repo_id"
            ):
                conn.execute(
                    "UPDATE repos SET group_id = ? WHERE id = ?",
                    (row["group_id"], row["repo_id"]),
                )
    conn.commit()


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> bool:
    columns = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True
    return False
