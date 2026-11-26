"""共享仓库：local 裸仓库、群组 ACL。"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def repo_path(git_root: str | Path, slug: str) -> Path:
    if not SLUG_RE.match(str(slug or "")):
        raise ValueError("仓库 slug 只允许小写字母、数字、连字符，且以字母/数字开头")
    root = Path(git_root).resolve()
    path = (root / f"{slug}.git").resolve()
    if root != path.parent:
        raise ValueError("仓库路径越界")
    return path


def _run_git(args: list[str], *, cwd: str | Path | None = None,
             input_bytes: bytes | None = None, env: dict | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        input=input_bytes,
        capture_output=True,
        env=env,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败: {result.stderr.decode('utf-8', 'replace')[:500]}"
        )
    return result.stdout


def _init_bare_repo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError("仓库目录已存在")
    _run_git(["init", "--bare", str(path)])
    _run_git(["--git-dir", str(path), "config", "http.receivepack", "true"])

    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "cili-server",
            "GIT_AUTHOR_EMAIL": "cili-server@localhost",
            "GIT_COMMITTER_NAME": "cili-server",
            "GIT_COMMITTER_EMAIL": "cili-server@localhost",
        }
    )
    blob = _run_git(
        ["--git-dir", str(path), "hash-object", "-w", "--stdin"],
        input_bytes=b".cili/\n",
        env=env,
    ).strip()
    tree = _run_git(
        ["--git-dir", str(path), "mktree"],
        input_bytes=b"100644 blob " + blob + b"\t.gitignore\n",
        env=env,
    ).strip()
    commit = _run_git(
        ["--git-dir", str(path), "commit-tree", tree.decode(), "-m", "init"],
        env=env,
    ).strip()
    _run_git(
        ["--git-dir", str(path), "update-ref", "refs/heads/main", commit.decode()],
        env=env,
    )
    _run_git(
        ["--git-dir", str(path), "symbolic-ref", "HEAD", "refs/heads/main"],
        env=env,
    )


def create_repo(
    conn: sqlite3.Connection,
    *,
    git_root: str | Path,
    org_id: str,
    slug: str,
    name: str,
    group_ids: list[str] | None = None,
    backend: str = "local",
    upstream_url: str = "",
    upstream_username: str = "",
    upstream_password: str = "",
    now: int | None = None,
) -> sqlite3.Row:
    slug = str(slug or "").strip()
    backend = str(backend or "local").strip().lower()
    if backend not in {"local", "forward"}:
        raise ValueError("backend 必须是 local 或 forward")
    path = repo_path(git_root, slug)
    org = conn.execute(
        "SELECT id FROM organizations WHERE id = ?", (org_id,)
    ).fetchone()
    if org is None:
        raise ValueError("组织不存在")
    for group_id in group_ids or []:
        group = conn.execute(
            "SELECT org_id FROM groups WHERE id = ?", (group_id,)
        ).fetchone()
        if group is None or str(group["org_id"]) != str(org_id):
            raise ValueError("授权群组不存在或不属于同一组织")
    if backend == "forward":
        if not str(upstream_url or "").startswith(("http://", "https://")):
            raise ValueError("forward 仓库必须提供 http(s) upstream_url")
    else:
        _init_bare_repo(path)
    repo_id = "repo_" + uuid.uuid4().hex[:12]
    try:
        conn.execute(
            "INSERT INTO repos"
            " (id, org_id, slug, name, backend, upstream_url,"
            "  upstream_username, upstream_password, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                repo_id, org_id, slug, str(name), backend,
                str(upstream_url or ""), str(upstream_username or ""),
                str(upstream_password or ""),
                int(now if now is not None else time.time()),
            ),
        )
        for group_id in group_ids or []:
            conn.execute(
                "INSERT OR IGNORE INTO repo_groups (repo_id, group_id, can_write)"
                " VALUES (?, ?, 1)",
                (repo_id, group_id),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        if backend == "local":
            shutil.rmtree(path, ignore_errors=True)
        raise
    return get_repo(conn, repo_id)


def update_repo(
    conn: sqlite3.Connection,
    repo_id: str,
    *,
    git_root: str | Path | None = None,
    name: str | None = None,
    backend: str | None = None,
    upstream_url: str | None = None,
    upstream_username: str | None = None,
    upstream_password: str | None = None,
) -> sqlite3.Row:
    repo = get_repo(conn, repo_id)
    if repo is None:
        raise ValueError("仓库不存在")
    new_backend = backend if backend is not None else str(repo["backend"])
    if new_backend not in {"local", "forward"}:
        raise ValueError("backend 必须是 local 或 forward")
    new_upstream = (
        upstream_url if upstream_url is not None else str(repo["upstream_url"])
    )
    if new_backend == "forward" and not str(new_upstream).startswith(
        ("http://", "https://")
    ):
        raise ValueError("forward 仓库必须提供 http(s) upstream_url")
    if new_backend == "local" and str(repo["backend"]) != "local":
        if git_root is None:
            raise ValueError("切换到 local 需要 git_root")
        path = repo_path(git_root, str(repo["slug"]))
        if not path.exists():
            _init_bare_repo(path)

    fields: list[str] = []
    values: list[object] = []
    if name is not None:
        fields.append("name = ?")
        values.append(str(name))
    if backend is not None:
        if backend not in {"local", "forward"}:
            raise ValueError("backend 必须是 local 或 forward")
        fields.append("backend = ?")
        values.append(backend)
    if upstream_url is not None:
        if upstream_url and not str(upstream_url).startswith(("http://", "https://")):
            raise ValueError("upstream_url 必须是 http(s)")
        fields.append("upstream_url = ?")
        values.append(str(upstream_url))
    if upstream_username is not None:
        fields.append("upstream_username = ?")
        values.append(str(upstream_username))
    if upstream_password is not None:
        fields.append("upstream_password = ?")
        values.append(str(upstream_password))
    if fields:
        values.append(repo_id)
        conn.execute(f"UPDATE repos SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    return get_repo(conn, repo_id)


def get_repo(conn: sqlite3.Connection, repo_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()


def get_repo_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM repos WHERE slug = ?", (slug,)).fetchone()


def list_repos(conn: sqlite3.Connection, org_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM repos WHERE org_id = ? ORDER BY name, id", (org_id,)
        )
    )


def delete_repo(
    conn: sqlite3.Connection, repo_id: str, *, git_root: str | Path
) -> bool:
    repo = get_repo(conn, repo_id)
    if repo is None:
        return False
    conn.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
    conn.commit()
    if str(repo["backend"]) == "local":
        path = repo_path(git_root, str(repo["slug"]))
        shutil.rmtree(path, ignore_errors=True)
    return True


def bind_repo_group(
    conn: sqlite3.Connection, *, repo_id: str, group_id: str, can_write: bool = True
) -> None:
    repo = get_repo(conn, repo_id)
    group = conn.execute(
        "SELECT org_id FROM groups WHERE id = ?", (group_id,)
    ).fetchone()
    if repo is None or group is None or str(group["org_id"]) != str(repo["org_id"]):
        raise ValueError("仓库或群组不存在，或不属于同一组织")
    conn.execute(
        "INSERT INTO repo_groups (repo_id, group_id, can_write) VALUES (?, ?, ?)"
        " ON CONFLICT(repo_id, group_id) DO UPDATE SET can_write = excluded.can_write",
        (repo_id, group_id, 1 if can_write else 0),
    )
    conn.commit()


def unbind_repo_group(
    conn: sqlite3.Connection, *, repo_id: str, group_id: str
) -> bool:
    cursor = conn.execute(
        "DELETE FROM repo_groups WHERE repo_id = ? AND group_id = ?",
        (repo_id, group_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def list_repo_groups(conn: sqlite3.Connection, repo_id: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT rg.*, g.name AS group_name FROM repo_groups rg"
            " JOIN groups g ON g.id = rg.group_id"
            " WHERE rg.repo_id = ? ORDER BY g.name, g.id",
            (repo_id,),
        )
    )


def repo_access(
    conn: sqlite3.Connection, *, person_id: str, repo_id: str
) -> str | None:
    """返回 'write' / 'read' / None。"""
    row = conn.execute(
        "SELECT MAX(rg.can_write) AS can_write FROM repo_groups rg"
        " JOIN group_members gm ON gm.group_id = rg.group_id"
        " WHERE rg.repo_id = ? AND gm.person_id = ?",
        (repo_id, person_id),
    ).fetchone()
    if row is None or row["can_write"] is None:
        return None
    return "write" if int(row["can_write"]) else "read"
