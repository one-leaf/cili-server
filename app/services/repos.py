"""共享仓库：local 裸仓库、群组 ACL。"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterator

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
    now: int | None = None,
) -> sqlite3.Row:
    slug = str(slug or "").strip()
    path = repo_path(git_root, slug)
    org = conn.execute(
        "SELECT id FROM organizations WHERE id = ?", (org_id,)
    ).fetchone()
    if org is None:
        raise ValueError("组织不存在")
    _init_bare_repo(path)
    repo_id = "repo_" + uuid.uuid4().hex[:12]
    try:
        conn.execute(
            "INSERT INTO repos (id, org_id, slug, name, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                repo_id, org_id, slug, str(name),
                int(now if now is not None else time.time()),
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        shutil.rmtree(path, ignore_errors=True)
        raise
    return get_repo(conn, repo_id)


def update_repo(
    conn: sqlite3.Connection,
    repo_id: str,
    *,
    name: str | None = None,
) -> sqlite3.Row:
    repo = get_repo(conn, repo_id)
    if repo is None:
        raise ValueError("仓库不存在")
    if name is not None:
        conn.execute(
            "UPDATE repos SET name = ? WHERE id = ?", (str(name), repo_id)
        )
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
    path = repo_path(git_root, str(repo["slug"]))
    shutil.rmtree(path, ignore_errors=True)
    return True


def _bundle_workspaces_for_person(
    conn: sqlite3.Connection, *, person_id: str, org_id: str
) -> Iterator[list]:
    rows = conn.execute(
        "SELECT b.workspaces_json FROM bundle_bindings bb"
        " JOIN bundles b ON b.id = bb.bundle_id"
        " JOIN group_members gm ON gm.group_id = bb.target_id"
        " WHERE bb.target_type = 'group' AND gm.person_id = ?"
        "   AND b.org_id = ?",
        (person_id, org_id),
    )
    for row in rows:
        try:
            entries = json.loads(row["workspaces_json"] or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(entries, list):
            yield entries


def repo_groups(conn: sqlite3.Connection, repo_id: str) -> list[dict]:
    """仓库当前对哪些群组开放（由配置包绑定推导，只读展示）。"""
    repo = get_repo(conn, repo_id)
    if repo is None:
        return []
    slug = str(repo["slug"])
    rows = conn.execute(
        "SELECT DISTINCT g.id, g.name, b.workspaces_json FROM groups g"
        " JOIN bundle_bindings bb"
        "   ON bb.target_type = 'group' AND bb.target_id = g.id"
        " JOIN bundles b ON b.id = bb.bundle_id"
        " WHERE b.org_id = ?",
        (str(repo["org_id"]),),
    )
    granted: dict[str, str] = {}
    for row in rows:
        try:
            entries = json.loads(row["workspaces_json"] or "[]")
        except (TypeError, ValueError):
            continue
        if isinstance(entries, list) and any(
            isinstance(entry, dict) and str(entry.get("key") or "") == slug
            for entry in entries
        ):
            granted[str(row["id"])] = str(row["name"])
    return [
        {"id": group_id, "name": name}
        for group_id, name in sorted(granted.items(), key=lambda item: item[1])
    ]


def repo_access(
    conn: sqlite3.Connection, *, person_id: str, repo_id: str
) -> str | None:
    """仓库访问权限：属于配置包绑定的群组且配置包含该工作区即读写。"""
    repo = get_repo(conn, repo_id)
    if repo is None:
        return None
    slug = str(repo["slug"])
    for entries in _bundle_workspaces_for_person(
        conn, person_id=person_id, org_id=str(repo["org_id"])
    ):
        if any(
            isinstance(entry, dict) and str(entry.get("key") or "") == slug
            for entry in entries
        ):
            return "write"
    return None
