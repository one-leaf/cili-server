"""管理端：登录、人员/群组/配置包/设备管理。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

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
from app.services.bundles import (
    bind_bundle,
    create_bundle,
    delete_bundle,
    list_bindings,
    list_bundles,
    unbind_bundle,
    update_bundle,
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
    update_person,
)
from app.services.repos import (
    bind_repo_group,
    create_repo,
    delete_repo,
    get_repo,
    list_repo_groups,
    list_repos,
    unbind_repo_group,
    update_repo,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
ADMIN_COOKIE = "cili_admin"


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


def _person(row) -> dict:
    return {
        "id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "name": str(row["name"]),
        "phone": str(row["phone"]),
        "enabled": bool(row["enabled"]),
        "note": str(row["note"]),
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


def _bundle(row) -> dict:
    try:
        model = json.loads(row["model_json"]) if row["model_json"] else None
    except (TypeError, ValueError):
        model = None
    try:
        workspaces = (
            json.loads(row["workspaces_json"]) if row["workspaces_json"] else []
        )
    except (TypeError, ValueError):
        workspaces = []
    return {
        "id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "name": str(row["name"]),
        "priority": int(row["priority"]),
        "model": model,
        "workspaces": workspaces,
        "is_default": bool(row["is_default"]),
        "updated_at": _fmt_ts(row["updated_at"]),
    }


def _binding(row) -> dict:
    return {
        "id": str(row["id"]),
        "bundle_id": str(row["bundle_id"]),
        "bundle_name": str(row["bundle_name"]) if "bundle_name" in row.keys() else "",
        "target_type": str(row["target_type"]),
        "target_id": str(row["target_id"]),
        "priority": int(row["priority"]),
    }


def _repo(row) -> dict:
    return {
        "id": str(row["id"]),
        "org_id": str(row["org_id"]),
        "slug": str(row["slug"]),
        "name": str(row["name"]),
        "backend": str(row["backend"]),
        "upstream_url": str(row["upstream_url"]),
        "upstream_username": str(row["upstream_username"]),
        "has_upstream_password": bool(row["upstream_password"]),
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


class PersonCreate(BaseModel):
    org_id: str
    name: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=1, max_length=32)
    enabled: bool = True
    note: str = ""


class PersonUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    enabled: bool | None = None
    note: str | None = None


class GroupCreate(BaseModel):
    org_id: str
    name: str = Field(min_length=1, max_length=64)
    note: str = ""


class GroupUpdate(BaseModel):
    name: str | None = None
    note: str | None = None


class GroupMemberRequest(BaseModel):
    person_id: str


class BundleCreate(BaseModel):
    org_id: str
    name: str = Field(min_length=1, max_length=64)
    model: dict | None = None
    workspaces: list[dict] | None = None
    priority: int = 0
    is_default: bool = False


class BundleUpdate(BaseModel):
    name: str | None = None
    model: dict | None = None
    workspaces: list[dict] | None = None
    priority: int | None = None
    is_default: bool | None = None


class BindRequest(BaseModel):
    target_type: str
    target_id: str
    priority: int = 0


class RepoCreate(BaseModel):
    org_id: str
    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    group_ids: list[str] = []
    backend: str = "local"
    upstream_url: str = ""
    upstream_username: str = ""
    upstream_password: str = ""


class RepoUpdate(BaseModel):
    name: str | None = None
    backend: str | None = None
    upstream_url: str | None = None
    upstream_username: str | None = None
    upstream_password: str | None = None


class RepoGroupRequest(BaseModel):
    group_id: str
    can_write: bool = True


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
        "SELECT id, name, slug, created_at, config_version FROM organizations"
        " ORDER BY name, id"
    ))
    return {
        "orgs": [
            {
                "id": str(row["id"]),
                "name": str(row["name"]),
                "slug": str(row["slug"]),
                "config_version": str(row["config_version"]),
            }
            for row in rows
        ]
    }


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


# ── 配置包 ────────────────────────────────────────────


@router.get("/bundles")
async def bundles(org_id: str, request: Request, _admin=Depends(require_admin)):
    _require_org(_conn(request), org_id)
    return {"bundles": [_bundle(row) for row in list_bundles(_conn(request), org_id)]}


@router.post("/bundles")
async def create_bundle_route(
    payload: BundleCreate, request: Request, _admin=Depends(require_admin)
):
    _check_origin(request)
    _require_org(_conn(request), payload.org_id)
    row = _service_call(
        create_bundle, _conn(request),
        org_id=payload.org_id, name=payload.name, model=payload.model,
        workspaces=payload.workspaces, priority=payload.priority,
        is_default=payload.is_default,
    )
    _audit(request, _admin, payload.org_id, "bundle_create",
           target=str(row["id"]), detail=payload.name)
    return {"bundle": _bundle(row)}


@router.patch("/bundles/{bundle_id}")
async def update_bundle_route(
    bundle_id: str, payload: BundleUpdate, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    row = _service_call(
        update_bundle, _conn(request), bundle_id,
        name=payload.name, model=payload.model, workspaces=payload.workspaces,
        priority=payload.priority, is_default=payload.is_default,
    )
    _audit(request, _admin, str(row["org_id"]), "bundle_update",
           target=bundle_id)
    return {"bundle": _bundle(row)}


@router.delete("/bundles/{bundle_id}")
async def delete_bundle_route(
    bundle_id: str, request: Request, _admin=Depends(require_admin)
):
    _check_origin(request)
    conn = _conn(request)
    bundle = conn.execute(
        "SELECT org_id FROM bundles WHERE id = ?", (bundle_id,)
    ).fetchone()
    if bundle is None or not delete_bundle(conn, bundle_id):
        raise ApiError(404, "bundle_not_found")
    _audit(request, _admin, str(bundle["org_id"]), "bundle_delete",
           target=bundle_id)
    return {"ok": True}


@router.get("/bindings")
async def bindings(org_id: str, request: Request, _admin=Depends(require_admin)):
    _require_org(_conn(request), org_id)
    return {
        "bindings": [_binding(row) for row in list_bindings(_conn(request), org_id)]
    }


@router.post("/bundles/{bundle_id}/bind")
async def bind_bundle_route(
    bundle_id: str, payload: BindRequest, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    conn = _conn(request)
    row = _service_call(
        bind_bundle, conn,
        bundle_id=bundle_id, target_type=payload.target_type,
        target_id=payload.target_id, priority=payload.priority,
    )
    bundle = conn.execute(
        "SELECT org_id FROM bundles WHERE id = ?", (bundle_id,)
    ).fetchone()
    _audit(request, _admin, str(bundle["org_id"]) if bundle else "",
           "bundle_bind", target=str(row["id"]),
           detail=f"{payload.target_type}:{payload.target_id}")
    return {"binding": _binding(row)}


@router.delete("/bindings/{binding_id}")
async def unbind_bundle_route(
    binding_id: str, request: Request, _admin=Depends(require_admin)
):
    _check_origin(request)
    conn = _conn(request)
    row = conn.execute(
        "SELECT b.org_id FROM bundle_bindings bb"
        " JOIN bundles b ON b.id = bb.bundle_id WHERE bb.id = ?",
        (binding_id,),
    ).fetchone()
    if row is None or not unbind_bundle(conn, binding_id):
        raise ApiError(404, "binding_not_found")
    _audit(request, _admin, str(row["org_id"]), "bundle_unbind",
           target=binding_id)
    return {"ok": True}


# ── 仓库 ──────────────────────────────────────────────


@router.get("/repos")
async def repos(org_id: str, request: Request, _admin=Depends(require_admin)):
    _require_org(_conn(request), org_id)
    return {"repos": [_repo(row) for row in list_repos(_conn(request), org_id)]}


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
        group_ids=payload.group_ids, backend=payload.backend,
        upstream_url=payload.upstream_url,
        upstream_username=payload.upstream_username,
        upstream_password=payload.upstream_password,
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
        update_repo, _conn(request), repo_id,
        git_root=request.app.state.settings.git_root,
        name=payload.name, backend=payload.backend,
        upstream_url=payload.upstream_url,
        upstream_username=payload.upstream_username,
        upstream_password=payload.upstream_password,
    )
    _audit(request, _admin, str(row["org_id"]), "repo_update",
           target=repo_id)
    return {"repo": _repo(row)}


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


@router.get("/repos/{repo_id}/groups")
async def repo_groups(
    repo_id: str, request: Request, _admin=Depends(require_admin)
):
    if get_repo(_conn(request), repo_id) is None:
        raise ApiError(404, "repo_not_found")
    return {
        "groups": [
            {
                "repo_id": str(row["repo_id"]),
                "group_id": str(row["group_id"]),
                "group_name": str(row["group_name"]),
                "can_write": bool(row["can_write"]),
            }
            for row in list_repo_groups(_conn(request), repo_id)
        ]
    }


@router.post("/repos/{repo_id}/groups")
async def bind_repo_group_route(
    repo_id: str, payload: RepoGroupRequest, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    _service_call(
        bind_repo_group, _conn(request),
        repo_id=repo_id, group_id=payload.group_id, can_write=payload.can_write,
    )
    repo = get_repo(_conn(request), repo_id)
    _audit(request, _admin, str(repo["org_id"]) if repo else "", "repo_acl_add",
           target=repo_id,
           detail=f"{payload.group_id}:{'write' if payload.can_write else 'read'}")
    return {"ok": True}


@router.delete("/repos/{repo_id}/groups/{group_id}")
async def unbind_repo_group_route(
    repo_id: str, group_id: str, request: Request,
    _admin=Depends(require_admin),
):
    _check_origin(request)
    conn = _conn(request)
    repo = get_repo(conn, repo_id)
    if repo is None or not unbind_repo_group(
        conn, repo_id=repo_id, group_id=group_id
    ):
        raise ApiError(404, "repo_group_not_found")
    _audit(request, _admin, str(repo["org_id"]), "repo_acl_remove",
           target=repo_id, detail=group_id)
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
