"""审计日志：只记录元数据，不记录验证码/令牌/pack 内容。"""

from __future__ import annotations

import logging
import sqlite3
import time

logger = logging.getLogger(__name__)


def record(
    conn: sqlite3.Connection,
    *,
    action: str,
    actor: str = "",
    org_id: str = "",
    target: str = "",
    detail: str = "",
    ip: str = "",
    now: int | None = None,
) -> None:
    try:
        conn.execute(
            "INSERT INTO audit_log"
            " (org_id, at, actor, action, target, detail, ip)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                str(org_id or ""),
                int(now if now is not None else time.time()),
                str(actor or "")[:200],
                str(action or "")[:80],
                str(target or "")[:200],
                str(detail or "")[:2000],
                str(ip or "")[:64],
            ),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 —— 审计失败不能影响主流程
        logger.warning("[audit] 写入失败: %s", action, exc_info=True)


def list_entries(
    conn: sqlite3.Connection,
    *,
    org_id: str | None = None,
    limit: int = 100,
    before_id: int | None = None,
) -> list[sqlite3.Row]:
    size = min(max(int(limit or 100), 1), 200)
    clauses: list[str] = []
    values: list[object] = []
    if org_id:
        clauses.append("org_id = ?")
        values.append(org_id)
    if before_id:
        clauses.append("id < ?")
        values.append(int(before_id))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    values.append(size)
    return list(
        conn.execute(
            f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ?", values
        )
    )
