"""cili-server 设置加载。

当前只包含短信配置；后续的监听地址、数据库、组织等配置继续加在这里。
真实 ``config.json`` 不进 Git，仓库只保留 ``config.example.json``。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from app.sms.base import SmsConfigError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_DATABASE_PATH = PROJECT_ROOT / "data" / "cili-server.db"
DEFAULT_GIT_ROOT = PROJECT_ROOT / "data" / "git"

SUPPORTED_PROVIDERS = frozenset({"tencent", "mock", "webhook", "aliyun"})


@dataclass(frozen=True)
class TencentSmsConfig:
    """腾讯云 SendSms 配置。

    SecretId / SecretKey 只从 config.json 或环境变量读取；repr 中隐藏。
    """

    secret_id: str = field(repr=False)
    secret_key: str = field(repr=False)
    sdk_app_id: str
    sign_name: str
    template_id: str
    region: str = "ap-guangzhou"
    timeout_seconds: float = 10.0
    retries: int = 0
    endpoint: str = "sms.tencentcloudapi.com"
    version: str = "2021-01-11"
    action: str = "SendSms"
    session_context: str = "cili-org"


@dataclass(frozen=True)
class SmsConfig:
    provider: str = "tencent"
    tencent: TencentSmsConfig | None = None


@dataclass(frozen=True)
class VerificationConfig:
    """验证码策略；code_pepper 用于 HMAC，不落库、不进日志。"""

    code_pepper: str = field(repr=False, default="")
    code_ttl_seconds: int = 300
    max_attempts: int = 5
    cooldown_seconds: int = 60
    per_hour_limit: int = 5
    per_day_limit: int = 10
    ip_hour_limit: int = 30


@dataclass(frozen=True)
class AdminConfig:
    initial_username: str = ""
    initial_password: str = field(repr=False, default="")
    cookie_secure: bool = False
    session_ttl_seconds: int = 12 * 3600


@dataclass(frozen=True)
class Settings:
    sms: SmsConfig
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    admin: AdminConfig = field(default_factory=AdminConfig)
    database_path: str = str(DEFAULT_DATABASE_PATH)
    git_root: str = str(DEFAULT_GIT_ROOT)
    trust_forwarded_for: bool = False



def load_settings(path: str | Path | None = None) -> Settings:
    """读取 config.json，并应用环境变量覆盖。

    环境变量优先于配置文件：
    ``CILI_SMS_PROVIDER``、``CILI_SMS_TENCENT_SECRET_ID``、
    ``CILI_SMS_TENCENT_SECRET_KEY``、``CILI_SMS_TENCENT_SDK_APP_ID``、
    ``CILI_SMS_TENCENT_SIGN_NAME``、``CILI_SMS_TENCENT_TEMPLATE_ID``、
    ``CILI_SMS_TENCENT_REGION``。
    """
    config_path = Path(
        path or os.environ.get("CILI_SERVER_CONFIG", str(DEFAULT_CONFIG_PATH))
    )
    if not config_path.is_file():
        raise SmsConfigError(f"配置文件不存在: {config_path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmsConfigError(f"读取配置文件失败: {exc}") from exc

    sms_raw = raw.get("sms") if isinstance(raw, dict) else None
    if not isinstance(sms_raw, dict):
        sms_raw = {}

    provider = str(
        os.environ.get("CILI_SMS_PROVIDER") or sms_raw.get("provider") or "tencent"
    ).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise SmsConfigError(
            f"不支持的短信 provider: {provider!r}（可选：{', '.join(sorted(SUPPORTED_PROVIDERS))}）"
        )

    tencent_raw = sms_raw.get("tencent")
    if not isinstance(tencent_raw, dict):
        tencent_raw = {}

    def _value(env_name: str, key: str, default: str = "") -> str:
        return str(
            os.environ.get(env_name) or tencent_raw.get(key) or default
        ).strip()

    secret_id = _value("CILI_SMS_TENCENT_SECRET_ID", "secret_id")
    secret_key = _value("CILI_SMS_TENCENT_SECRET_KEY", "secret_key")
    sdk_app_id = _value("CILI_SMS_TENCENT_SDK_APP_ID", "sdk_app_id")
    sign_name = _value("CILI_SMS_TENCENT_SIGN_NAME", "sign_name")
    template_id = _value("CILI_SMS_TENCENT_TEMPLATE_ID", "template_id")
    region = _value("CILI_SMS_TENCENT_REGION", "region", "ap-guangzhou")

    if provider == "tencent":
        missing = [
            name
            for name, value in (
                ("secret_id", secret_id),
                ("secret_key", secret_key),
                ("sdk_app_id", sdk_app_id),
                ("sign_name", sign_name),
                ("template_id", template_id),
            )
            if not value
        ]
        if missing:
            raise SmsConfigError(
                "tencent 短信配置缺少: " + ", ".join(missing)
                + "（在 cili-server 的 config.json 或环境变量中设置）"
            )

    timeout_seconds = _positive_float(tencent_raw.get("timeout_seconds"), 10.0)
    retries = _non_negative_int(tencent_raw.get("retries"), 0)

    tencent = TencentSmsConfig(
        secret_id=secret_id,
        secret_key=secret_key,
        sdk_app_id=sdk_app_id,
        sign_name=sign_name,
        template_id=template_id,
        region=region,
        timeout_seconds=timeout_seconds,
        retries=retries,
    ) if provider == "tencent" else None

    verification_raw = sms_raw.get("verification")
    if not isinstance(verification_raw, dict):
        verification_raw = {}

    code_pepper = str(
        os.environ.get("CILI_SMS_CODE_PEPPER")
        or verification_raw.get("code_pepper")
        or (secret_key if provider == "tencent" else "")
    )
    verification = VerificationConfig(
        code_pepper=code_pepper,
        code_ttl_seconds=_positive_int(
            verification_raw.get("code_ttl_seconds"), 300
        ),
        max_attempts=_positive_int(verification_raw.get("max_attempts"), 5),
        cooldown_seconds=_positive_int(
            verification_raw.get("cooldown_seconds"), 60
        ),
        per_hour_limit=_positive_int(verification_raw.get("per_hour_limit"), 5),
        per_day_limit=_positive_int(verification_raw.get("per_day_limit"), 10),
        ip_hour_limit=_positive_int(verification_raw.get("ip_hour_limit"), 30),
    )

    database_raw = raw.get("database")
    if not isinstance(database_raw, dict):
        database_raw = {}
    raw_database_path = str(
        os.environ.get("CILI_SERVER_DB")
        or database_raw.get("path")
        or DEFAULT_DATABASE_PATH
    )
    database_path = Path(raw_database_path).expanduser()
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / database_path

    git_raw = raw.get("git")
    if not isinstance(git_raw, dict):
        git_raw = {}
    raw_git_root = str(
        os.environ.get("CILI_GIT_ROOT")
        or git_raw.get("root")
        or DEFAULT_GIT_ROOT
    )
    git_root = Path(raw_git_root).expanduser()
    if not git_root.is_absolute():
        git_root = PROJECT_ROOT / git_root

    server_raw = raw.get("server")
    if not isinstance(server_raw, dict):
        server_raw = {}
    env_trust = str(os.environ.get("CILI_TRUST_FORWARDED_FOR") or "").strip().lower()
    trust_forwarded_for = (
        env_trust in {"1", "true", "yes", "on"}
        or str(server_raw.get("trust_forwarded_for") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    admin_raw = raw.get("admin")
    if not isinstance(admin_raw, dict):
        admin_raw = {}
    admin = AdminConfig(
        initial_username=str(
            os.environ.get("CILI_ADMIN_USERNAME")
            or admin_raw.get("initial_username")
            or ""
        ).strip(),
        initial_password=str(
            os.environ.get("CILI_ADMIN_PASSWORD")
            or admin_raw.get("initial_password")
            or ""
        ),
        cookie_secure=(
            str(os.environ.get("CILI_ADMIN_COOKIE_SECURE") or "").strip().lower()
            in {"1", "true", "yes", "on"}
            or str(admin_raw.get("cookie_secure") or "").strip().lower()
            in {"1", "true", "yes", "on"}
        ),
        session_ttl_seconds=_positive_int(
            admin_raw.get("session_ttl_seconds"), 12 * 3600
        ),
    )

    return Settings(
        sms=SmsConfig(provider=provider, tencent=tencent),
        verification=verification,
        admin=admin,
        database_path=str(database_path),
        git_root=str(git_root),
        trust_forwarded_for=trust_forwarded_for,
    )


def _positive_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _positive_int(value, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
