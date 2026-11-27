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


MODEL_PROTOCOLS = frozenset(
    {"anthropic", "openai", "openai-responses", "openai-compatible"}
)
MODEL_AUTH_TYPES = frozenset({"auto", "bearer", "x-api-key", "none"})
MODEL_TYPES = frozenset({"reasoning", "chat"})
MODEL_MODALITIES = frozenset({"text", "image", "audio", "video", "pdf"})


def _positive_int(value: Any, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} 必须是整数") from None
    if parsed <= 0:
        raise ValueError(f"{field_name} 必须大于 0")
    return parsed


def normalize_model(model: dict[str, Any] | None) -> dict[str, Any] | None:
    """校验配置包的模型块；字段与 cili 的 provider / 模型设置一致。

    空对象表示清除模型配置。``api_key`` 不在这里配置（按人员下发）。
    """
    if not model:
        return None
    if not isinstance(model, dict):
        raise ValueError("model 必须是 JSON 对象")

    result = dict(model)
    result.setdefault("mode", "direct")
    protocol = str(result.get("protocol") or "anthropic").strip()
    if protocol not in MODEL_PROTOCOLS:
        raise ValueError(f"不支持的模型协议: {protocol}")
    result["protocol"] = protocol

    auth_type = str(result.get("auth_type") or "auto").strip()
    if auth_type not in MODEL_AUTH_TYPES:
        raise ValueError(f"不支持的认证方式: {auth_type}")
    result["auth_type"] = auth_type

    base_url = str(result.get("base_url") or "").strip()
    if base_url and not base_url.startswith(("http://", "https://")):
        raise ValueError("base_url 必须是 http(s) 地址")
    if base_url:
        result["base_url"] = base_url

    models_raw = result.get("models")
    if not isinstance(models_raw, dict) or not models_raw:
        raise ValueError("模型配置至少需要一个模型")
    models: dict[str, Any] = {}
    for alias, spec in models_raw.items():
        alias = str(alias or "").strip()
        if not alias:
            raise ValueError("模型别名不能为空")
        if not isinstance(spec, dict):
            raise ValueError(f"模型 {alias} 的配置必须是对象")
        entry = dict(spec)
        entry["name"] = str(entry.get("name") or "").strip()
        for field_name in ("max_context_tokens", "max_output_tokens"):
            if entry.get(field_name) not in (None, ""):
                entry[field_name] = _positive_int(entry[field_name], field_name)
        model_type = str(entry.get("model_type") or "reasoning").strip()
        if model_type not in MODEL_TYPES:
            raise ValueError(f"模型 {alias} 的 model_type 必须是 reasoning 或 chat")
        entry["model_type"] = model_type
        if entry.get("input") is not None:
            modalities = [str(item) for item in entry["input"]]
            if "text" not in modalities:
                raise ValueError(f"模型 {alias} 的 input 必须包含 text")
            unknown = [item for item in modalities if item not in MODEL_MODALITIES]
            if unknown:
                raise ValueError(
                    f"模型 {alias} 含不支持的输入模态: {', '.join(unknown)}"
                )
            entry["input"] = modalities
        models[alias] = entry
    result["models"] = models
    # 组织配置不下发角色绑定（由客户端自行选择）
    result.pop("roles", None)
    return result


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
            _dump_json(normalize_model(model), ""),
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
        values.append(_dump_json(normalize_model(model), ""))
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
    token: str = "",
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

    # 模型块注入组织 provider 名与该人员的 Key，客户端直接物化为 provider
    if isinstance(model, dict):
        model = dict(model)
        model.pop("roles", None)
        model["provider_key"] = f"org-{org['slug']}"
        person_key = str(person["model_api_key"] or "")
        if person_key:
            model["api_key"] = person_key

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

    # 补齐 git 块：远程地址、用户名与服务端请求所用设备令牌
    web_url = str(org["web_url"] or "").rstrip("/")
    for workspace in workspaces:
        git_raw = workspace.get("git")
        git = dict(git_raw) if isinstance(git_raw, dict) else {}
        remote = str(git.get("remote_url") or "")
        if not remote and web_url:
            remote = f"{web_url}/git/{workspace.get('key', '')}.git"
        git["remote_url"] = remote
        git.setdefault("branch", "main")
        git["username"] = "org"
        git.setdefault("sync_mode", "remote_wins")
        if token:
            git["password"] = token
        workspace["git"] = git

    version = f"{org['config_version']}-{person['updated_at']}"
    return {
        "version": version,
        "org": {
            "id": str(org["slug"]),
            "name": str(org["name"]),
            "web_url": str(org["web_url"]),
        },
        "user": {
            "id": person_id,
            "name": str(person["name"]),
            "phone": str(person["phone"]),
            "email": str(person["email"]),
            "groups": [str(group["name"]) for group in groups],
        },
        "model": model,
        "workspaces": workspaces,
    }
