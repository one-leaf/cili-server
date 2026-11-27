"""配置下发：Bearer 设备令牌 + ETag/304。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app.errors import ApiError
from app.services.delivery import resolve_config
from app.services.identity import (
    authenticate_token,
    get_org,
)

router = APIRouter(prefix="/api/v1", tags=["config"])


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


def _normalize_etag(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    return value.strip('"').strip()


@router.get("/config")
async def get_config(request: Request):
    conn = request.app.state.conn
    token = _bearer_token(request)
    device = authenticate_token(conn, token, require_enabled=False)
    if device is None:
        raise ApiError(401, "token_invalid")

    org = get_org(conn, device.org_id)
    if org is None:
        raise ApiError(404, "org_not_found")
    person = conn.execute(
        "SELECT * FROM people WHERE id = ? AND org_id = ?",
        (device.person_id, device.org_id),
    ).fetchone()
    if person is None or not int(person["enabled"]):
        raise ApiError(403, "person_disabled")

    payload = resolve_config(conn, org=org, person=person, token=token)
    if payload.get("model") is None:
        raise ApiError(
            409,
            "org_model_not_configured",
            message="尚未配置组织模型，请先在「设置 → 模型设置」中配置",
        )
    etag = f'"{payload["version"]}"'
    if _normalize_etag(request.headers.get("if-none-match", "")) == payload["version"]:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(content=payload, headers={"ETag": etag})
