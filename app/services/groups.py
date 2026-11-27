"""群组与成员关系。"""

from __future__ import annotations

import sqlite3
import time
import uuid

from app.services.identity import bump_config_version


def create_group(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    name: str,
    note: str = "",
    now: int | None = None,
) -> sqlite3.Row:
    group_id = "g_" + uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO groups (id, org_id, name, note, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (group_id, org_id, name, note, int(now if now is not None else time.time())),
    )
    bump_config_version(conn, org_id, now=now)
    return conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def get_group(conn: sqlite3.Connection, group_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def list_org_groups(conn: sqlite3.Connection, org_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM groups WHERE org_id = ? ORDER BY name, id", (org_id,)
        )
    )


def update_group(
    conn: sqlite3.Connection,
    group_id: str,
    *,
    name: str | None = None,
    note: str | None = None,
) -> sqlite3.Row:
    group = get_group(conn, group_id)
    if group is None:
        raise ValueError("群组不存在")
    fields: list[str] = []
    values: list[object] = []
    if name is not None:
        fields.append("name = ?")
        values.append(str(name))
    if note is not None:
        fields.append("note = ?")
        values.append(str(note))
    if fields:
        values.append(group_id)
        conn.execute(
            f"UPDATE groups SET {', '.join(fields)} WHERE id = ?", values
        )
        bump_config_version(conn, str(group["org_id"]))
    return get_group(conn, group_id)


def delete_group(conn: sqlite3.Connection, group_id: str) -> bool:
    group = get_group(conn, group_id)
    if group is None:
        return False
    conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    bump_config_version(conn, str(group["org_id"]))
    return True


def list_group_members(
    conn: sqlite3.Connection, group_id: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT p.* FROM people p"
            " JOIN group_members gm ON gm.person_id = p.id"
            " WHERE gm.group_id = ? ORDER BY p.name, p.id",
            (group_id,),
        )
    )


def add_group_member(
    conn: sqlite3.Connection, *, group_id: str, person_id: str
) -> None:
    group = get_group(conn, group_id)
    if group is None:
        raise ValueError("群组不存在")
    conn.execute(
        "INSERT OR IGNORE INTO group_members (group_id, person_id)"
        " VALUES (?, ?)",
        (group_id, person_id),
    )
    bump_config_version(conn, str(group["org_id"]))
    conn.commit()


def remove_group_member(
    conn: sqlite3.Connection, *, group_id: str, person_id: str
) -> None:
    group = get_group(conn, group_id)
    if group is None:
        raise ValueError("群组不存在")
    conn.execute(
        "DELETE FROM group_members WHERE group_id = ? AND person_id = ?",
        (group_id, person_id),
    )
    bump_config_version(conn, str(group["org_id"]))
    conn.commit()


def list_person_groups(
    conn: sqlite3.Connection, person_id: str
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT g.* FROM groups g"
            " JOIN group_members gm ON gm.group_id = g.id"
            " WHERE gm.person_id = ?"
            " ORDER BY g.name, g.id",
            (person_id,),
        )
    )
