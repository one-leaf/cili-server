"""短信验证码：生成、限流、发送与校验。

验证码只存 HMAC 哈希；TTL、尝试次数、手机号/IP 限流落 SQLite。
provider 的网络调用在事务之外，避免长时间占库锁。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from app.settings import VerificationConfig
from app.sms.base import SmsError, SmsProvider, normalize_phone

logger = logging.getLogger(__name__)


class SmsVerificationError(SmsError):
    """验证码业务错误基类。"""


class SmsRateLimited(SmsVerificationError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"发送过于频繁，请 {self.retry_after} 秒后再试")


class SmsCodeExpired(SmsVerificationError):
    def __init__(self, message: str = "验证码不存在或已过期"):
        super().__init__(message)


class SmsCodeAttemptsExceeded(SmsVerificationError):
    def __init__(self, message: str = "验证码尝试次数过多，请重新获取"):
        super().__init__(message)


@dataclass(frozen=True)
class SendCodeResult:
    phone: str
    cooldown_seconds: int


def generate_code() -> str:
    """6 位数字验证码（使用 secrets，不用 random）。"""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(code: str, pepper: str) -> str:
    """HMAC-SHA256；pepper 只存在服务端。"""
    return hmac.new(
        (pepper or "").encode("utf-8"),
        str(code or "").encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


class SmsVerificationService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        provider: SmsProvider,
        config: VerificationConfig,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self._conn = conn
        self._provider = provider
        self._config = config
        self._clock = clock
        self._lock = threading.RLock()

    def send_code(
        self, phone: str, *, org_id: str = "", ip: str = ""
    ) -> SendCodeResult:
        normalized = normalize_phone(phone)
        now = int(self._clock())
        code = generate_code()
        code_hash = hash_code(code, self._config.code_pepper)
        code_id = uuid.uuid4().hex
        expires_at = now + self._config.code_ttl_seconds

        with self._lock:
            _begin(self._conn)
            try:
                self._clean_events(now)
                self._check_rate_limits(normalized, ip, now)
                # 同一组织 + 手机号只保留最新一条有效验证码
                self._conn.execute(
                    "DELETE FROM sms_codes WHERE org_id = ? AND phone = ?",
                    (org_id, normalized),
                )
                self._conn.execute(
                    "INSERT INTO sms_codes"
                    " (id, org_id, phone, code_hash, created_at, expires_at, attempts, ip)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (code_id, org_id, normalized, code_hash, now, expires_at, ip),
                )
                cursor = self._conn.execute(
                    "INSERT INTO sms_send_events (org_id, phone, ip, sent_at)"
                    " VALUES (?, ?, ?, ?)",
                    (org_id, normalized, ip, now),
                )
                event_id = int(cursor.lastrowid)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

        try:
            self._provider.send(normalized, code)
        except BaseException:
            # 发送失败不占用用户的重发额度，也清掉未生效的验证码。
            try:
                with self._lock:
                    _begin(self._conn)
                    self._conn.execute(
                        "DELETE FROM sms_codes WHERE id = ?", (code_id,)
                    )
                    self._conn.execute(
                        "DELETE FROM sms_send_events WHERE id = ?", (event_id,)
                    )
                    self._conn.commit()
            except Exception:  # noqa: BLE001
                logger.warning("[sms] 清理失败验证码记录失败", exc_info=True)
            raise

        return SendCodeResult(
            phone=normalized,
            cooldown_seconds=self._config.cooldown_seconds,
        )

    def verify_code(self, phone: str, code: str, *, org_id: str = "") -> bool:
        normalized = normalize_phone(phone)
        now = int(self._clock())
        with self._lock:
            _begin(self._conn)
            try:
                row = self._conn.execute(
                    "SELECT id, code_hash, expires_at, attempts"
                    " FROM sms_codes WHERE org_id = ? AND phone = ?"
                    " ORDER BY created_at DESC LIMIT 1",
                    (org_id, normalized),
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    raise SmsCodeExpired()

                if int(row["expires_at"]) <= now:
                    self._conn.execute(
                        "DELETE FROM sms_codes WHERE id = ?", (row["id"],)
                    )
                    self._conn.commit()
                    raise SmsCodeExpired()

                if int(row["attempts"]) >= self._config.max_attempts:
                    self._conn.execute(
                        "DELETE FROM sms_codes WHERE id = ?", (row["id"],)
                    )
                    self._conn.commit()
                    raise SmsCodeAttemptsExceeded()

                expected = str(row["code_hash"])
                actual = hash_code(code, self._config.code_pepper)
                if not hmac.compare_digest(expected, actual):
                    self._conn.execute(
                        "UPDATE sms_codes SET attempts = attempts + 1 WHERE id = ?",
                        (row["id"],),
                    )
                    self._conn.commit()
                    return False

                self._conn.execute(
                    "DELETE FROM sms_codes WHERE id = ?", (row["id"],)
                )
                self._conn.commit()
                return True
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    # ── 内部 ──────────────────────────────────────────────

    def _clean_events(self, now: int) -> None:
        self._conn.execute(
            "DELETE FROM sms_send_events WHERE sent_at < ?",
            (now - 86400 * 2,),
        )
        self._conn.execute(
            "DELETE FROM sms_codes WHERE expires_at <= ?", (now,)
        )

    def _check_rate_limits(self, phone: str, ip: str, now: int) -> None:
        last = self._conn.execute(
            "SELECT sent_at FROM sms_send_events"
            " WHERE phone = ? ORDER BY sent_at DESC LIMIT 1",
            (phone,),
        ).fetchone()
        if last is not None:
            elapsed = now - int(last["sent_at"])
            if elapsed < self._config.cooldown_seconds:
                raise SmsRateLimited(self._config.cooldown_seconds - elapsed)

        self._check_window(
            "phone", phone, now, 3600, self._config.per_hour_limit
        )
        self._check_window(
            "phone", phone, now, 86400, self._config.per_day_limit
        )
        if ip:
            self._check_window("ip", ip, now, 3600, self._config.ip_hour_limit)

    def _check_window(
        self,
        column: str,
        value: str,
        now: int,
        window: int,
        limit: int,
    ) -> None:
        start = now - window
        row = self._conn.execute(
            f"SELECT COUNT(*) AS cnt, MIN(sent_at) AS first_at"
            f" FROM sms_send_events WHERE {column} = ? AND sent_at > ?",
            (value, start),
        ).fetchone()
        if int(row["cnt"]) < limit:
            return
        first_at = int(row["first_at"]) if row["first_at"] is not None else now
        retry_after = window - (now - first_at)
        raise SmsRateLimited(retry_after)


def _begin(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.rollback()
    conn.execute("BEGIN IMMEDIATE")
