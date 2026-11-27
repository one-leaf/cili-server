"""管理端：登录、人员/群组/模型设置/仓库/设备管理。"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from app.db import init_schema

from app.errors import ApiError
from app.services.audit import list_entries as list_audit_entries
from app.services.audit import record as record_audit
from app.services.admin_auth import (
    AdminIdentity,
    authenticate_admin,
    authenticate_admin_session,
    create_admin_session,
    revoke_admin_session,
)
from app.services.groups import (
    add_group_member,
    create_group,
    delete_group,
    get_group,
    list_group_members,
    list_org_groups,
    remove_group_member,
    update_group,
)
from app.services.identity import (
    create_person,
    delete_person,
    get_org,
    list_devices,
    list_people,
    revoke_device,
    update_org,
    update_person,
)
from app.services.model_config import (
    get_org_model,
    update_org_model,
)
from app.services.repos import (
    create_repo,
    delete_repo,
    get_repo,
    list_repos,
    repo_group,
    set_repo_group,
    update_repo,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
ADMIN_COOKIE = "cili_admin"
logger = logging.getLogger(__name__)

RESTORE_MAX_BYTES = 200 * 1024 * 1024
_RESTORE_REQUIRED_TABLES = {"organizations", "people", "admin_users"}
_RESTORE_TABLES = (
    "organizations",
    "admin_users",
    "people",
    "groups",
    "group_members",
    "bundles",
    "bundle_bindings",
    "devices",
    "repos",
    "audit_log",
    "admin_sessions",
    "sms_codes",
    "sms_send_events",
)
_restore_lock = threading.Lock()


# ── 依赖 ──────────────────────────────────────────────


def _conn(request: Request) -> sqlite3.Connection:
    return request.app.state.conn


def require_admin(request: Request) -> AdminIdentity:
    token = request.cookies.get(ADMIN_COOKIE, "")
    admin = authenticate_admin_session(_conn(request), token)
    if admin is None:
        raise ApiError(401, "admin_unauthorized")
    return admin


def _check_origin(request: Request) -> None:
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    if not origin:
        return
    host = urlparse(origin).hostname or ""
    expected = (request.headers.get("host") or "").split(":", 1)[0]
    if not host or (expected and host != expected):
        raise ApiError(403, "origin_forbidden")


def _snapshot_db(conn: sqlite3.Connection, target: Path) -> None:
    """生成一致的 SQLite 快照（在线备份 API）。"""
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
    finally:
        dest.close()


def _pre_restore_backup(settings, conn: sqlite3.Connection) -> str:
    db_path = str(getattr(settings, "database_path", "") or "")
    if not db_path or db_path == ":memory:":
        return ""
    target_dir = Path(db_path).parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"pre-restore-{time.strftime('%Y%m%d-%H%M%S')}.db"
    _snapshot_db(conn, target)
    return str(target)


def _validate_backup(path: Path) -> None:
    try:
        test = sqlite3.connect(str(path))
    except sqlite3.DatabaseError:
        raise ApiError(400, "invalid_backup", message="不是有效的 SQLite 数据库") from None
    try:
        tables = {
            row[0]
            for row in test.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = _RESTORE_REQUIRED_TABLES - tables
        if missing:
            raise ApiError(
                400,
                "invalid_backup",
                message="备份缺少必要表: " + ", ".join(sorted(missing)),
            )
        # 兼容旧版本备份：补齐新增列/表
        init_schema(test)
    except sqlite3.DatabaseError:
        raise ApiError(400, "invalid_backup", message="备份文件无法读取") from None
    finally:
        test.close()


def _apply_restore(conn: sqlite3.Connection, path: Path) -> None:
    conn.execute("PRAGMA foreign_keys=OFF")
    attached = False
    try:
        conn.execute("ATTACH DATABASE ? AS imported", (str(path),))
        attached = True
        conn.execute("BEGIN IMMEDIATE")
        try:
            for table in _RESTORE_TABLES:
                dest_cols = [
                    row[1]
                    for row in conn.execute(f"PRAGMA main.table_info({table})")
                ]
                src_cols = {
                    row[1]
                    for row in conn.execute(f"PRAGMA imported.table_info({table})")
                }
                if not dest_cols:
                    continue
                cols = [col for col in dest_cols if col in src_cols]
                conn.execute(f"DELETE FROM main.{table}")
                if cols:
                    spec = ", ".join(cols)
                    conn.execute(
                        f"INSERT INTO main.{table} ({spec})"
                        f" SELECT {spec} FROM imported.{table}"
                    )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        if attached:
            conn.execute("DETACH DATABASE imported")
        conn.execute("PRAGMA foreign_keys=ON")


def _service_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as exc:
        raise ApiError(400, "invalid_request", message=str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise ApiError(409, "conflict") from exc


def _require_org(conn: sqlite3.Connection, org_id: str):
    if not org_id:
        raise ApiError(400, "org_required")
    org = get_org(conn, org_id)
    if org is None:
        raise ApiError(404, "org_not_found")
    return org


def _audit(
    request: Request,
    admin: AdminIdentity,
    org_id: str,
    action: str,
    *,
    target: str = "",
    detail: str = "",
) -> None:
    record_audit(
        _conn(request),
        org_id=org_id,
        actor=admin.username,
        action=action,
        target=target,
        detail=detail,
        ip=request.client.host if request.client else "",
    )


def _fmt_ts(value) -> str:
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


def _mask_secret(value: str) -> str:
    value = str(value or "")
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def _org_sms_payload(org) -> dict:
    raw = str(org["sms_config"] or "")
    data: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
        except (TypeError, ValueError):
            data = {}
    return {
        "provider": str(org["sms_provider"] or ""),
        "configured": bool(data.get("secret_id") and data.get("secret_key")),
        "tencent": {
            "secret_id_masked": _mask_secret(str(data.get("secret_id") or "")),
            "has_secret_key": bool(data.get("secret_key")),
            "sdk_app_id": str(data.get("sdk_app_id") or ""),
            "sign_name": str(data.get("sign_name") or ""),
            "template_id": str(data.get("template_id") or ""),
            "region": str(data.get("region") or "ap-guangzhou"),
        },
    }


def _person(row) -> dict:
    return {
        "id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "name": str(row["name"]),
        "phone": str(row["phone"]),
        "enabled": bool(row["enabled"]),
        "note": str(row["note"]),
        "model_api_key": str(row["model_api_key"]),
        "created_at": _fmt_ts(row["created_at"]),
        "updated_at": _fmt_ts(row["updated_at"]),
    }


def _group(row) -> dict:
    return {
        "id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "name": str(row["name"]),
        "note": str(row["note"]),
        "created_at": _fmt_ts(row["created_at"]),
    }


def _repo(row) -> dict:
    return {
        "id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "slug": str(row["slug"]),
        "name": str(row["name"]),
        "created_at": _fmt_ts(row["created_at"]),
    }


def _device(row) -> dict:
    return {
        "id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "person_id": str(row["person_id"]),
        "person_name": str(row["person_name"]) if "person_name" in row.keys() else "",
        "phone": str(row["phone"]) if "phone" in row.keys() else "",
        "name": str(row["name"]),
        "created_at": _fmt_ts(row["created_at"]),
        "last_seen_at": _fmt_ts(row["last_seen_at"]),
        "expires_at": _fmt_ts(row["expires_at"]),
        "revoked": bool(row["revoked"]),
    }


# ── 登录 ──────────────────────────────────────────────


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class OrgUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    web_url: str | None = Field(default=None, max_length=200)


class SmsSettingsUpdate(BaseModel):
    secret_id: str | None = Field(default=None, max_length=200)
    secret_key: str | None = Field(default=None, max_length=200)
    sdk_app_id: str | None = Field(default=None, max_length=64)
    sign_name: str | None = Field(default=None, max_length=128)
    template_id: str | None = Field(default=None, max_length=64)
    region: str | None = Field(default=None, max_length=64)


class PersonCreate(BaseModel):
    org_id: str
    name: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=1, max_length=32)
    enabled: bool = True
    note: str = ""
    model_api_key: str = Field(default="", max_length=512)


class PersonUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    enabled: bool | None = None
    note: str | None = None
    model_api_key: str | None = Field(default=None, max_length=512)


class GroupCreate(BaseModel):
    org_id: str
    name: str = Field(min_length=1, max_length=64)
    note: str = ""


class GroupUpdate(BaseModel):
    name: str | None = None
    note: str | None = None


class GroupMemberRequest(BaseModel):
    person_id: str


class ModelSettingsUpdate(BaseModel):
    model: dict | None = None


class RepoCreate(BaseModel):
    org_id: str
    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    group_id: str = ""


class RepoUpdate(BaseModel):
    name: str | None = None


class RepoGroupUpdate(BaseModel):
    group_id: str = ""


@router.post("/login")
async def login(payload: LoginRequest, request: Request):
    _check_origin(request)
    admin = authenticate_admin(_conn(request), payload.username, payload.password)
    if admin is None:
        record_audit(
            _conn(request),
            actor=str(payload.username or "")[:200],
            action="admin_login_failed",
            target=str(payload.username or "")[:200],
            ip=request.client.host if request.client else "",
        )
        raise ApiError(401, "invalid_credentials")
    record_audit(
        _conn(request),
        actor=admin.username,
        action="admin_login",
        ip=request.client.host if request.client else "",
    )
    settings = request.app.state.settings
    token, expires_at = create_admin_session(
        _conn(request),
        admin.id,
        ttl_seconds=settings.admin.session_ttl_seconds,
    )
    response = JSONResponse(
        {"ok": True, "expires_at": _fmt_ts(expires_at)}
    )
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        max_age=settings.admin.session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.admin.cookie_secure,
        path="/",
    )
    return response


@router.post("/logout")
async def logout(request: Request, _admin: AdminIdentity = Depends(require_admin)):
    _check_origin(request)
    _audit(request, _admin, "", "admin_logout")
    revoke_admin_session(_conn(request), request.cookies.get(ADMIN_COOKIE, ""))
    response = JSONResponse({"ok": True})
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return response


@router.get("/me")
async def me(admin: AdminIdentity = Depends(require_admin)):
    return {"id": admin.id, "username": admin.username}


# ── 组织 ──────────────────────────────────────────────


@router.get("/orgs")
async def orgs(request: Request, _admin=Depends(require_admin)):
    rows = list(_conn(request).execute(
        "SELECT id, name, slug, created_at, config_version, web_url"
        " FROM organizations ORDER BY name, id"
    ))
    return {
        "orgs": [
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "slug": str(row["slug"]),
                "config_version": str(row["config_version"]),
                "web_url": str(row["web_url"]),
            }
            for row in rows
        ]
    }


@router.patch("/orgs/{org_id}")
async def update_org_route(
    org_id: str, payload: OrgUpdate, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    name = payload.name.strip() if payload.name is not None else None
    web_url = payload.web_url.strip() if payload.web_url is not None else None
    if name is not None and not name:
        raise ApiError(400, "invalid_request", message="组织名称不能为空")
    if web_url and not web_url.startswith(("http://", "https://")):
        raise ApiError(400, "invalid_request", message="对外地址必须是 http(s)")
    _require_org(_conn(request), org_id)
    row = _service_call(
        update_org, _conn(request), org_id, name=name, web_url=web_url
    )
    _audit(
        request, _admin, org_id, "org_update", target=org_id,
        detail=" ".join(part for part in (name, web_url) if part),
    )
    return {
        "org": {
            "id": str(row["id"]),
            "name": str(row["name"]),
            "slug": str(row["slug"]),
            "config_version": str(row["config_version"]),
            "web_url": str(row["web_url"]),
        }
    }


# ── 短信（腾讯云） ─────────────────────────────────────


@router.get("/orgs/{org_id}/sms")
async def get_org_sms(
    org_id: str, request: Request, _admin=Depends(require_admin)
):
    org = _require_org(_conn(request), org_id)
    return {"sms": _org_sms_payload(org)}


@router.put("/orgs/{org_id}/sms")
async def put_org_sms(
    org_id: str, payload: SmsSettingsUpdate, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    conn = _conn(request)
    org = _require_org(conn, org_id)
    merged: dict = {}
    raw = str(org["sms_config"] or "")
    if raw:
        try:
            existing = json.loads(raw)
            if isinstance(existing, dict):
                merged.update(existing)
        except (TypeError, ValueError):
            merged = {}
    for field in ("sdk_app_id", "sign_name", "template_id", "region"):
        value = getattr(payload, field)
        if value is not None:
            merged[field] = value.strip()
    # 密钥留空表示不修改
    for field in ("secret_id", "secret_key"):
        value = getattr(payload, field)
        if value:
            merged[field] = value.strip()
    merged["provider"] = "tencent"
    if not str(merged.get("region") or "").strip():
        merged["region"] = "ap-guangzhou"
    missing = [
        field
        for field in (
            "secret_id", "secret_key", "sdk_app_id", "sign_name", "template_id"
        )
        if not str(merged.get(field) or "").strip()
    ]
    if missing:
        raise ApiError(
            400, "invalid_request", message="缺少字段: " + ", ".join(missing)
        )
    conn.execute(
        "UPDATE organizations SET sms_provider = 'tencent', sms_config = ?"
        " WHERE id = ?",
        (json.dumps(merged, ensure_ascii=False), org_id),
    )
    conn.commit()
    resolver = getattr(request.app.state, "sms_resolver", None)
    if resolver is not None:
        resolver.invalidate(org_id)
    _audit(request, _admin, org_id, "sms_update", target=org_id, detail="tencent")
    return {"sms": _org_sms_payload(_require_org(conn, org_id))}


@router.delete("/orgs/{org_id}/sms")
async def delete_org_sms(
    org_id: str, request: Request, _admin=Depends(require_admin)
):
    _check_origin(request)
    conn = _conn(request)
    _require_org(conn, org_id)
    conn.execute(
        "UPDATE organizations SET sms_provider = '', sms_config = ''"
        " WHERE id = ?",
        (org_id,),
    )
    conn.commit()
    resolver = getattr(request.app.state, "sms_resolver", None)
    if resolver is not None:
        resolver.invalidate(org_id)
    _audit(request, _admin, org_id, "sms_reset", target=org_id)
    return {"ok": True}


# ── 人员 ──────────────────────────────────────────────


@router.get("/people")
async def people(org_id: str, request: Request, _admin=Depends(require_admin)):
    _require_org(_conn(request), org_id)
    return {"people": [_person(row) for row in list_people(_conn(request), org_id)]}


@router.post("/people")
async def create_person_route(
    payload: PersonCreate, request: Request, _admin=Depends(require_admin)
):
    _check_origin(request)
    conn = _conn(request)
    _require_org(conn, payload.org_id)
    row = _service_call(
        create_person,
        conn,
        org_id=payload.org_id,
        name=payload.name,
        phone=payload.phone,
        enabled=payload.enabled,
        note=payload.note,
        model_api_key=payload.model_api_key.strip(),
    )
    _audit(request, _admin, payload.org_id, "person_create",
           target=str(row["id"]), detail=payload.name)
    return {"person": _person(row)}


@router.patch("/people/{person_id}")
async def update_person_route(
    person_id: str,
    payload: PersonUpdate,
    request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    row = _service_call(
        update_person,
        _conn(request),
        person_id,
        name=payload.name,
        phone=payload.phone,
        enabled=payload.enabled,
        note=payload.note,
        model_api_key=(
            payload.model_api_key.strip()
            if payload.model_api_key is not None
            else None
        ),
    )
    _audit(request, _admin, str(row["org_id"]), "person_update",
           target=person_id)
    return {"person": _person(row)}


@router.delete("/people/{person_id}")
async def delete_person_route(
    person_id: str, request: Request, _admin=Depends(require_admin)
):
    _check_origin(request)
    conn = _conn(request)
    person = conn.execute(
        "SELECT org_id FROM people WHERE id = ?", (person_id,)
    ).fetchone()
    if person is None or not delete_person(conn, person_id):
        raise ApiError(404, "person_not_found")
    _audit(request, _admin, str(person["org_id"]), "person_delete",
           target=person_id)
    return {"ok": True}


# ── 群组 ──────────────────────────────────────────────


@router.get("/groups")
async def groups(org_id: str, request: Request, _admin=Depends(require_admin)):
    _require_org(_conn(request), org_id)
    return {"groups": [_group(row) for row in list_org_groups(_conn(request), org_id)]}


@router.post("/groups")
async def create_group_route(
    payload: GroupCreate, request: Request, _admin=Depends(require_admin)
):
    _check_origin(request)
    _require_org(_conn(request), payload.org_id)
    row = _service_call(
        create_group,
        _conn(request),
        org_id=payload.org_id,
        name=payload.name,
        note=payload.note,
    )
    _audit(request, _admin, payload.org_id, "group_create",
           target=str(row["id"]), detail=payload.name)
    return {"group": _group(row)}


@router.patch("/groups/{group_id}")
async def update_group_route(
    group_id: str, payload: GroupUpdate, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    row = _service_call(
        update_group, _conn(request), group_id,
        name=payload.name, note=payload.note,
    )
    _audit(request, _admin, str(row["org_id"]), "group_update",
           target=group_id)
    return {"group": _group(row)}


@router.delete("/groups/{group_id}")
async def delete_group_route(
    group_id: str, request: Request, _admin=Depends(require_admin)
):
    _check_origin(request)
    conn = _conn(request)
    group = get_group(conn, group_id)
    if group is None or not delete_group(conn, group_id):
        raise ApiError(404, "group_not_found")
    _audit(request, _admin, str(group["org_id"]), "group_delete",
           target=group_id)
    return {"ok": True}


@router.get("/groups/{group_id}/members")
async def group_members(
    group_id: str, request: Request, _admin=Depends(require_admin)
):
    if get_group(_conn(request), group_id) is None:
        raise ApiError(404, "group_not_found")
    return {
        "people": [_person(row) for row in list_group_members(_conn(request), group_id)]
    }


@router.post("/groups/{group_id}/members")
async def add_member_route(
    group_id: str, payload: GroupMemberRequest, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    group = get_group(_conn(request), group_id)
    if group is None:
        raise ApiError(404, "group_not_found")
    _service_call(
        add_group_member, _conn(request),
        group_id=group_id, person_id=payload.person_id,
    )
    _audit(request, _admin, str(group["org_id"]), "group_member_add",
           target=group_id, detail=payload.person_id)
    return {"ok": True}


@router.delete("/groups/{group_id}/members/{person_id}")
async def remove_member_route(
    group_id: str, person_id: str, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    group = get_group(_conn(request), group_id)
    if group is None:
        raise ApiError(404, "group_not_found")
    _service_call(
        remove_group_member, _conn(request),
        group_id=group_id, person_id=person_id,
    )
    _audit(request, _admin, str(group["org_id"]), "group_member_remove",
           target=group_id, detail=person_id)
    return {"ok": True}


# ── 模型设置（组织级，所有人共用） ───────────────────


@router.get("/orgs/{org_id}/model")
async def get_model_settings(
    org_id: str, request: Request, _admin=Depends(require_admin)
):
    org = _require_org(_conn(request), org_id)
    return {"model": get_org_model(_conn(request), org)}


@router.put("/orgs/{org_id}/model")
async def update_model_settings(
    org_id: str, payload: ModelSettingsUpdate, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    conn = _conn(request)
    _require_org(conn, org_id)
    model = _service_call(update_org_model, conn, org_id, payload.model)
    _audit(
        request, _admin, org_id, "org_model_update", target=org_id,
        detail=",".join(sorted((model or {}).get("models", {}).keys())),
    )
    return {"model": model}


# ── 仓库 ──────────────────────────────────────────────


@router.get("/repos")
async def repos(org_id: str, request: Request, _admin=Depends(require_admin)):
    _require_org(_conn(request), org_id)
    conn = _conn(request)
    items = []
    for row in list_repos(conn, org_id):
        item = _repo(row)
        item["group"] = repo_group(conn, str(row["id"]))
        items.append(item)
    return {"repos": items}


@router.post("/repos")
async def create_repo_route(
    payload: RepoCreate, request: Request, _admin=Depends(require_admin)
):
    _check_origin(request)
    _require_org(_conn(request), payload.org_id)
    row = _service_call(
        create_repo, _conn(request),
        git_root=request.app.state.settings.git_root,
        org_id=payload.org_id, slug=payload.slug, name=payload.name,
        group_id=payload.group_id,
    )
    _audit(request, _admin, payload.org_id, "repo_create",
           target=str(row["id"]), detail=payload.slug)
    return {"repo": _repo(row)}


@router.patch("/repos/{repo_id}")
async def update_repo_route(
    repo_id: str, payload: RepoUpdate, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    row = _service_call(
        update_repo, _conn(request), repo_id, name=payload.name
    )
    _audit(request, _admin, str(row["org_id"]), "repo_update",
           target=repo_id)
    return {"repo": _repo(row)}


@router.put("/repos/{repo_id}/group")
async def set_repo_group_route(
    repo_id: str, payload: RepoGroupUpdate, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    conn = _conn(request)
    row = _service_call(
        set_repo_group, conn, repo_id=repo_id, group_id=payload.group_id
    )
    group = repo_group(conn, repo_id)
    _audit(
        request, _admin, str(row["org_id"]), "repo_group_update",
        target=repo_id, detail=(group or {}).get("name", ""),
    )
    return {"repo": {**_repo(row), "group": group}}


@router.delete("/repos/{repo_id}")
async def delete_repo_route(
    repo_id: str, request: Request, _admin=Depends(require_admin)
):
    _check_origin(request)
    conn = _conn(request)
    repo = get_repo(conn, repo_id)
    if repo is None or not delete_repo(
        conn, repo_id, git_root=request.app.state.settings.git_root
    ):
        raise ApiError(404, "repo_not_found")
    _audit(request, _admin, str(repo["org_id"]), "repo_delete",
           target=repo_id, detail=str(repo["slug"]))
    return {"ok": True}




# ── 审计 ──────────────────────────────────────────────


@router.get("/audit")
async def audit(
    request: Request,
    org_id: str = "",
    limit: int = 100,
    before_id: int | None = None,
    _admin=Depends(require_admin),
):
    rows = list_audit_entries(
        _conn(request), org_id=org_id or None, limit=limit, before_id=before_id
    )
    return {
        "entries": [
            {
                "id": int(row["id"]),
                "org_id": str(row["org_id"]),
                "at": _fmt_ts(row["at"]),
                "actor": str(row["actor"]),
                "action": str(row["action"]),
                "target": str(row["target"]),
                "detail": str(row["detail"]),
                "ip": str(row["ip"]),
            }
            for row in rows
        ]
    }


# ── 设备 ──────────────────────────────────────────────


@router.get("/devices")
async def devices(org_id: str, request: Request, _admin=Depends(require_admin)):
    _require_org(_conn(request), org_id)
    return {
        "devices": [_device(row) for row in list_devices(_conn(request), org_id)]
    }


@router.post("/devices/{device_id}/revoke")
async def revoke_device_route(
    device_id: str, request: Request, _admin=Depends(require_admin)
):
    _check_origin(request)
    conn = _conn(request)
    row = conn.execute(
        "SELECT org_id FROM devices WHERE id = ?", (device_id,)
    ).fetchone()
    if row is None or not revoke_device(conn, device_id):
        raise ApiError(404, "device_not_found")
    _audit(request, _admin, str(row["org_id"]), "device_revoke",
           target=device_id)
    return {"ok": True}


# ── 备份与恢复 ────────────────────────────────────────


@router.get("/backup")
async def download_backup(request: Request, _admin=Depends(require_admin)):
    conn = _conn(request)
    fd, raw_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    path = Path(raw_path)
    try:
        _snapshot_db(conn, path)
    except Exception as exc:  # noqa: BLE001
        path.unlink(missing_ok=True)
        logger.error("[admin] 生成备份失败", exc_info=True)
        raise ApiError(500, "backup_failed", message="生成备份失败") from exc
    _audit(request, _admin, "", "backup_export")
    filename = f"cili-server-{time.strftime('%Y%m%d-%H%M%S')}.db"
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=filename,
        background=BackgroundTask(lambda: path.unlink(missing_ok=True)),
    )


@router.post("/restore")
async def restore_backup(
    request: Request,
    file: UploadFile = File(...),
    _admin: AdminIdentity = Depends(require_admin),
):
    _check_origin(request)
    data = await file.read(RESTORE_MAX_BYTES + 1)
    if len(data) > RESTORE_MAX_BYTES:
        raise ApiError(413, "backup_too_large", message="备份文件过大")
    if not data.startswith(b"SQLite format 3\x00"):
        raise ApiError(400, "invalid_backup", message="不是有效的 SQLite 备份文件")

    fd, raw_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    path = Path(raw_path)
    try:
        path.write_bytes(data)
        _validate_backup(path)
        conn = _conn(request)
        if not _restore_lock.acquire(timeout=30):
            raise ApiError(409, "restore_busy", message="另一个恢复任务正在进行")
        try:
            try:
                pre_backup = _pre_restore_backup(
                    request.app.state.settings, conn
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("[admin] 恢复前备份失败", exc_info=True)
                raise ApiError(
                    500, "backup_failed", message="恢复前备份失败，已中止"
                ) from exc
            try:
                _apply_restore(conn, path)
            except Exception as exc:  # noqa: BLE001
                logger.error("[admin] 恢复数据库失败", exc_info=True)
                raise ApiError(
                    500, "restore_failed", message="恢复数据库失败"
                ) from exc
        finally:
            _restore_lock.release()

        resolver = getattr(request.app.state, "sms_resolver", None)
        if resolver is not None:
            resolver.close()
        _audit(
            request, _admin, "", "backup_restore",
            detail=Path(pre_backup).name if pre_backup else "",
        )
        return {"ok": True, "pre_restore_backup": pre_backup}
    finally:
        path.unlink(missing_ok=True)
