"""短信验证码登录：发送、校验与设备令牌。"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.errors import ApiError
from app.services.groups import list_person_groups
from app.services.identity import (
    authenticate_token,
    create_device,
    find_person,
    get_org_by_ref,
    revoke_token,
)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
logger = logging.getLogger(__name__)


class SmsSendRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=1, max_length=32)


class SmsVerifyRequest(BaseModel):
    org_id: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=1, max_length=32)
    code: str = Field(min_length=1, max_length=16)
    device_name: str = Field(default="", max_length=120)


def _service(request: Request):
    return request.app.state.sms_service


def _conn(request: Request):
    return request.app.state.conn


def _client_ip(request: Request) -> str:
    # 只有显式开启 trust_forwarded_for 才取 X-Forwarded-For 第一段；
    # 直接暴露时该头可伪造，默认不信任。
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and settings.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


@router.post("/sms/send")
async def send_sms(payload: SmsSendRequest, request: Request):
    conn = _conn(request)
    org = get_org_by_ref(conn, payload.org_id)
    if org is None:
        raise ApiError(404, "org_not_found", message="组织不存在或已下线，请重新连接")
    org_id = str(org["id"])

    # 按需求返回明确提示（不再静默成功）：未登记 / 已停用分别给出中文说明。
    person = find_person(conn, org_id, payload.phone, enabled_only=False)
    if person is None:
        raise ApiError(
            404,
            "phone_not_registered",
            message="找不到用户，请输入正确的手机号",
        )
    if not int(person["enabled"]):
        raise ApiError(
            403,
            "person_disabled",
            message="该用户已被停用，请联系管理员",
        )

    result = _service(request).send_code(
        payload.phone,
        org_id=org_id,
        ip=_client_ip(request),
    )
    return {"ok": True, "cooldown_seconds": result.cooldown_seconds}


@router.post("/sms/verify")
async def verify_sms(payload: SmsVerifyRequest, request: Request):
    conn = _conn(request)
    org = get_org_by_ref(conn, payload.org_id)
    if org is None:
        raise ApiError(404, "org_not_found")
    org_id = str(org["id"])
    if not _service(request).verify_code(
        payload.phone, payload.code, org_id=org_id
    ):
        raise ApiError(400, "invalid_code")

    person = find_person(conn, org_id, payload.phone)
    if person is None:
        raise ApiError(403, "person_disabled")

    token, expires_at = create_device(
        conn,
        org_id=org_id,
        person_id=str(person["id"]),
        name=payload.device_name.strip(),
    )
    groups = list_person_groups(conn, str(person["id"]))
    return {
        "token": token,
        "expires_at": datetime.fromtimestamp(expires_at).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "org": {"id": str(org["slug"]), "name": str(org["name"])},
        "user": {
            "id": str(person["id"]),
            "name": str(person["name"]),
            "groups": [str(group["name"]) for group in groups],
        },
        "config_version": str(org["config_version"]),
    }


@router.post("/logout")
async def logout(request: Request):
    revoke_token(_conn(request), _bearer_token(request))
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    conn = _conn(request)
    device = authenticate_token(conn, _bearer_token(request))
    if device is None:
        raise ApiError(401, "token_invalid")
    groups = list_person_groups(conn, device.person_id)
    return {
        "org": {"id": device.org_slug, "name": device.org_name},
        "user": {"id": device.person_id, "name": device.person_name,
                 "phone": device.phone,
                 "groups": [str(group["name"]) for group in groups]},
        "config_version": device.config_version,
        "expires_at": datetime.fromtimestamp(device.expires_at).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    }
