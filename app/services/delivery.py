"""配置下发：组织级共享模型 + 人员群组对应的共享仓库。"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from app.services.groups import list_person_groups
from app.services.model_config import get_org_model


def provider_key_for(org) -> str:
    """组织 provider 名：直接使用企业名（仅替换空白和 /）；为空时回退 slug。"""
    name = re.sub(r"[\s/]+", "-", str(org["name"] or "").strip()).strip("-")
    return (name or str(org["slug"] or "default"))[:64]


def resolve_config(
    conn: sqlite3.Connection,
    *,
    org: sqlite3.Row,
    person: sqlite3.Row,
    token: str = "",
) -> dict[str, Any]:
    org_id = str(org["id"])
    person_id = str(person["id"])
    groups = list_person_groups(conn, person_id)

    model = get_org_model(conn, org)
    if isinstance(model, dict):
        model = dict(model)
        model.pop("roles", None)
        model["provider_key"] = provider_key_for(org)
        person_key = str(person["model_api_key"] or "")
        if person_key:
            model["api_key"] = person_key

    rows = conn.execute(
        "SELECT r.slug AS slug, r.name AS name,"
        "       g.id AS group_id, g.name AS group_name"
        " FROM repos r"
        " JOIN groups g ON g.id = r.group_id"
        " JOIN group_members gm ON gm.group_id = g.id"
        " WHERE gm.person_id = ? AND r.org_id = ?"
        " ORDER BY r.slug",
        (person_id, org_id),
    )

    web_url = str(org["web_url"] or "").rstrip("/")
    person_name = str(person["name"] or person_id)
    seen: set[str] = set()
    workspaces: list[dict[str, Any]] = []
    for row in rows:
        key = str(row["slug"])
        if key in seen:
            continue
        seen.add(key)
        git = {
            "remote_url": f"{web_url}/git/{key}.git" if web_url else "",
            "branch": "main",
            "username": person_name,
            "sync_mode": "remote_wins",
        }
        if token:
            git["password"] = token
        workspaces.append(
            {
                "key": key,
                "slug": key,
                "name": str(row["name"]),
                "memory_enabled": True,
                "group_id": str(row["group_id"]),
                "group_name": str(row["group_name"]),
                "git": git,
            }
        )

    return {
        "version": f"{org['config_version']}-{person['updated_at']}",
        "org": {
            "id": str(org["slug"]),
            "name": str(org["name"]),
            "web_url": str(org["web_url"]),
        },
        "user": {
            "id": person_id,
            "name": str(person["name"]),
            "phone": str(person["phone"]),
            "groups": [str(group["name"]) for group in groups],
        },
        "model": model,
        "workspaces": workspaces,
    }