"""组织级模型配置：所有人员共用，API Key 按人员下发。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.services.identity import bump_config_version

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
    """校验模型配置；字段与 cili 的 provider / 模型设置一致。

    空对象表示清除。``api_key`` 不在这里配置（按人员下发）。
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


def get_org_model(conn: sqlite3.Connection, org) -> dict[str, Any] | None:
    raw = str(org["model_config"] or "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data:
        return None
    return data


def update_org_model(
    conn: sqlite3.Connection,
    org_id: str,
    model: dict[str, Any] | None,
) -> dict[str, Any] | None:
    normalized = normalize_model(model)
    conn.execute(
        "UPDATE organizations SET model_config = ? WHERE id = ?",
        (json.dumps(normalized or {}, ensure_ascii=False), org_id),
    )
    bump_config_version(conn, org_id)
    return normalized