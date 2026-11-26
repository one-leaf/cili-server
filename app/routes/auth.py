"""短信验证码登录：发送、校验与设备令牌。"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.errors import ApiError
from app.services.identity import (
    authenticate_token,
    create_device,
    find_person,
    get_org,
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
    org = get_org(conn, payload.org_id)
    person = (
        find_person(conn, payload.org_id, payload.phone)
        if org is not None
        else None
    )
    settings = request.app.state.settings

    # 防枚举：组织不存在、手机号未登记或已停用都返回同样的 ok，且不发送。
    if org is None or person is None:
        return {
            "ok": True,
            "cooldown_seconds": settings.verification.cooldown_seconds,
        }

    result = _service(request).send_code(
        payload.phone,
        org_id=payload.org_id,
        ip=_client_ip(request),
    )
    return {"ok": True, "cooldown_seconds": result.cooldown_seconds}


@router.post("/sms/verify")
async def verify_sms(payload: SmsVerifyRequest, request: Request):
    conn = _conn(request)
    if not _service(request).verify_code(
        payload.phone, payload.code, org_id=payload.org_id
    ):
        raise ApiError(400, "invalid_code")

    org = get_org(conn, payload.org_id)
    if org is None:
        raise ApiError(404, "org_not_found")
    person = find_person(conn, payload.org_id, payload.phone)
    if person is None:
        raise ApiError(403, "person_disabled")

    token, expires_at = create_device(
        conn,
        org_id=payload.org_id,
        person_id=str(person["id"]),
        name=payload.device_name.strip(),
    )
    return {
        "token": token,
        "expires_at": datetime.fromtimestamp(expires_at).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "org": {"id": str(org["id"]), "name": str(org["name"])},
        "user": {
            "id": str(person["id"]),
            "name": str(person["name"]),
            "groups": [],
        },
        "config_version": str(org["config_version"]),
    }


@router.post("/logout")
async def logout(request: Request):
    revoke_token(_conn(request), _bearer_token(request))
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    device = authenticate_token(_conn(request), _bearer_token(request))
    if device is None:
        raise ApiError(401, "token_invalid")
    return {
        "org": {"id": device.org_id, "name": device.org_name},
        "user": {"id": device.person_id, "name": device.person_name,
                 "phone": device.phone, "groups": []},
        "config_version": device.config_version,
        "expires_at": datetime.fromtimestamp(device.expires_at).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    }
