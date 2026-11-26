"""配置包：模型 + 工作区定义，按人员/群组绑定解析。"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from app.services.groups import list_person_groups
from app.services.identity import bump_config_version


def _load_json(value: Any, default):
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _dump_json(value: Any, default: str) -> str:
    if value in (None, "", {}, []):
        return default
    return json.dumps(value, ensure_ascii=False)


def create_bundle(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    name: str,
    model: dict[str, Any] | None = None,
    workspaces: list[dict[str, Any]] | None = None,
    priority: int = 0,
    is_default: bool = False,
    now: int | None = None,
) -> sqlite3.Row:
    bundle_id = "b_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO bundles"
        " (id, org_id, name, priority, model_json, workspaces_json,"
        "  is_default, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            bundle_id,
            org_id,
            name,
            int(priority),
            _dump_json(model, ""),
            _dump_json(workspaces, "[]"),
            1 if is_default else 0,
            int(now if now is not None else time.time()),
        ),
    )
    bump_config_version(conn, org_id, now=now)
    return conn.execute(
        "SELECT * FROM bundles WHERE id = ?", (bundle_id,)
    ).fetchone()


def get_bundle(conn: sqlite3.Connection, bundle_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM bundles WHERE id = ?", (bundle_id,)
    ).fetchone()


def list_bundles(conn: sqlite3.Connection, org_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM bundles WHERE org_id = ? ORDER BY priority DESC, id",
            (org_id,),
        )
    )


def update_bundle(
    conn: sqlite3.Connection,
    bundle_id: str,
    *,
    name: str | None = None,
    model: dict[str, Any] | None = None,
    workspaces: list[dict[str, Any]] | None = None,
    priority: int | None = None,
    is_default: bool | None = None,
) -> sqlite3.Row:
    bundle = get_bundle(conn, bundle_id)
    if bundle is None:
        raise ValueError("配置包不存在")
    fields: list[str] = []
    values: list[Any] = []
    if name is not None:
        fields.append("name = ?")
        values.append(name)
    if model is not None:
        fields.append("model_json = ?")
        values.append(_dump_json(model, ""))
    if workspaces is not None:
        fields.append("workspaces_json = ?")
        values.append(_dump_json(workspaces, "[]"))
    if priority is not None:
        fields.append("priority = ?")
        values.append(int(priority))
    if is_default is not None:
        fields.append("is_default = ?")
        values.append(1 if is_default else 0)
    fields.append("updated_at = ?")
    values.append(int(time.time()))
    values.append(bundle_id)
    conn.execute(f"UPDATE bundles SET {', '.join(fields)} WHERE id = ?", values)
    bump_config_version(conn, str(bundle["org_id"]))
    return conn.execute(
        "SELECT * FROM bundles WHERE id = ?", (bundle_id,)
    ).fetchone()


def bind_bundle(
    conn: sqlite3.Connection,
    *,
    bundle_id: str,
    target_type: str,
    target_id: str,
    priority: int = 0,
) -> sqlite3.Row:
    if target_type not in ("group", "person"):
        raise ValueError("target_type 必须是 group 或 person")
    bundle = get_bundle(conn, bundle_id)
    if bundle is None:
        raise ValueError("配置包不存在")
    table = "groups" if target_type == "group" else "people"
    target = conn.execute(
        f"SELECT org_id FROM {table} WHERE id = ?", (target_id,)
    ).fetchone()
    if target is None or str(target["org_id"]) != str(bundle["org_id"]):
        raise ValueError("绑定目标不存在或不属于同一组织")

    existing = conn.execute(
        "SELECT id FROM bundle_bindings"
        " WHERE bundle_id = ? AND target_type = ? AND target_id = ?",
        (bundle_id, target_type, target_id),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE bundle_bindings SET priority = ? WHERE id = ?",
            (int(priority), existing["id"]),
        )
        binding_id = str(existing["id"])
    else:
        binding_id = "bb_" + uuid.uuid4().hex[:12]
        conn.execute(
            "INSERT INTO bundle_bindings"
            " (id, bundle_id, target_type, target_id, priority)"
            " VALUES (?, ?, ?, ?, ?)",
            (binding_id, bundle_id, target_type, target_id, int(priority)),
        )
    bump_config_version(conn, str(bundle["org_id"]))
    return conn.execute(
        "SELECT * FROM bundle_bindings WHERE id = ?", (binding_id,)
    ).fetchone()


def delete_bundle(conn: sqlite3.Connection, bundle_id: str) -> bool:
    bundle = get_bundle(conn, bundle_id)
    if bundle is None:
        return False
    conn.execute("DELETE FROM bundles WHERE id = ?", (bundle_id,))
    bump_config_version(conn, str(bundle["org_id"]))
    return True


def list_bindings(
    conn: sqlite3.Connection, org_id: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT bb.*, b.name AS bundle_name"
            " FROM bundle_bindings bb"
            " JOIN bundles b ON b.id = bb.bundle_id"
            " WHERE b.org_id = ?"
            " ORDER BY bb.target_type, bb.target_id, bb.priority DESC, bb.id",
            (org_id,),
        )
    )


def unbind_bundle(conn: sqlite3.Connection, binding_id: str) -> bool:
    row = conn.execute(
        "SELECT bb.id, b.org_id FROM bundle_bindings bb"
        " JOIN bundles b ON b.id = bb.bundle_id WHERE bb.id = ?",
        (binding_id,),
    ).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM bundle_bindings WHERE id = ?", (binding_id,))
    bump_config_version(conn, str(row["org_id"]))
    return True


def resolve_config(
    conn: sqlite3.Connection,
    *,
    org: sqlite3.Row,
    person: sqlite3.Row,
) -> dict[str, Any]:
    """按个人包 > 群组包 > 默认包解析模型；工作区只来自群组包。"""
    org_id = str(org["id"])
    person_id = str(person["id"])

    personal = list(
        conn.execute(
            "SELECT b.*, bb.priority AS binding_priority"
            " FROM bundle_bindings bb"
            " JOIN bundles b ON b.id = bb.bundle_id"
            " WHERE b.org_id = ? AND bb.target_type = 'person'"
            "   AND bb.target_id = ?"
            " ORDER BY bb.priority DESC, b.priority DESC, b.id",
            (org_id, person_id),
        )
    )
    groups = list_person_groups(conn, person_id)
    group_bundles = list(
        conn.execute(
            "SELECT b.*, bb.priority AS binding_priority,"
            "       g.id AS group_id, g.name AS group_name"
            " FROM bundle_bindings bb"
            " JOIN bundles b ON b.id = bb.bundle_id"
            " JOIN groups g ON g.id = bb.target_id"
            " JOIN group_members gm"
            "   ON gm.group_id = g.id AND gm.person_id = ?"
            " WHERE b.org_id = ? AND bb.target_type = 'group'"
            " ORDER BY bb.priority DESC, b.priority DESC, g.id",
            (person_id, org_id),
        )
    )
    default_bundle = conn.execute(
        "SELECT * FROM bundles WHERE org_id = ? AND is_default = 1"
        " ORDER BY priority DESC, id LIMIT 1",
        (org_id,),
    ).fetchone()

    model = None
    for bundle in [*personal, *group_bundles, default_bundle]:
        if bundle is None:
            continue
        candidate = _load_json(bundle["model_json"], None)
        if isinstance(candidate, dict) and candidate:
            model = candidate
            break

    seen: dict[str, dict[str, Any]] = {}
    for bundle in group_bundles:
        entries = _load_json(bundle["workspaces_json"], [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "").strip()
            if not key or key in seen:
                continue
            workspace = dict(entry)
            workspace["group_id"] = str(bundle["group_id"])
            workspace["group_name"] = str(bundle["group_name"])
            seen[key] = workspace

    workspaces = sorted(
        seen.values(),
        key=lambda item: (item.get("group_id", ""), item.get("key", "")),
    )
    version = f"{org['config_version']}-{person['updated_at']}"
    return {
        "version": version,
        "org": {"id": org_id, "name": str(org["name"])},
        "user": {
            "id": person_id,
            "name": str(person["name"]),
            "phone": str(person["phone"]),
            "groups": [str(group["name"]) for group in groups],
        },
        "model": model,
        "workspaces": workspaces,
    }
