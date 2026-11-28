"""共享仓库：local 裸仓库、群组 ACL。"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from app.services.identity import bump_config_version

logger = logging.getLogger(__name__)

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


# 共享工作区里 .cili 默认全部同步（会话/记忆/MCP/workflow/cron/tools/敏感词），
# 只有 tmp / cache / runtime 保留在本机。
_CILI_IGNORE_PREFIXES = (".cili/", "!.cili/")
_LOCAL_ONLY_CILI = (".cili/tmp/", ".cili/cache/", ".cili/runtime/")

# 新建共享仓库的初始 .gitignore：保留 .cili 共享语义，并忽略常见临时/构建文件
BASE_GITIGNORE = """# cili：仅以下三项保留本机，其余 .cili 内容参与同步（会话/记忆/MCP/workflow/cron/tools/敏感词）
.cili/tmp/
.cili/cache/
.cili/runtime/

# 系统 / 编辑器临时文件
.DS_Store
._*
.Spotlight-V100/
.Trashes/
.fseventsd/
[Tt]humbs.db
[Ee]hthumbs.db
[Dd]esktop.ini
$RECYCLE.BIN/
*.swp
*.swo
*~
*.bak
*.orig
*.rej
*.tmp
*.temp
.idea/
.vscode/
.vs/
*.suo
*.user

# Office / WPS 临时与锁文件（不影响正常的 .doc/.docx/.xls/.xlsx/.ppt/.pptx）
~$*
~WRL*.tmp
~*.tmp
.~lock.*#
*.wbk
*.xlk
*.asd

# 日志 / 环境变量（不忽略模板类 env 文件）
*.log
.env
.env.local
.env.*.local

# Python
__pycache__/
*.py[cod]
*.so
.pytest_cache/
.mypy_cache/
.ruff_cache/
.hypothesis/
.coverage
.coverage.*
htmlcov/
.venv/
venv/
.tox/
.nox/
*.egg-info/
.ipynb_checkpoints/

# Node / 前端
node_modules/
npm-debug.log*
yarn-debug.log*
yarn-error.log*
.pnpm-debug.log*
coverage/
.next/
.nuxt/
.svelte-kit/
.turbo/
.parcel-cache/
dist/
build/

# JVM / Rust / .NET 构建产物
*.class
target/
*.o
*.obj
*.pdb
"""
_BASE_GITIGNORE_RULES = tuple(
    line
    for line in BASE_GITIGNORE.splitlines()
    if line.strip() and not line.lstrip().startswith("#")
)
_BASE_GITIGNORE_COMMENTS = frozenset(
    line.strip()
    for line in BASE_GITIGNORE.splitlines()
    if line.lstrip().startswith("#")
)
# 旧版本基线的过宽规则，迁移时移除（如会误伤 .env.example）
_OBSOLETE_GITIGNORE_RULES = frozenset({".env.*"})


def _identity_env() -> dict:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "cili-server",
            "GIT_AUTHOR_EMAIL": "cili-server@localhost",
            "GIT_COMMITTER_NAME": "cili-server",
            "GIT_COMMITTER_EMAIL": "cili-server@localhost",
        }
    )
    return env


def _merge_shared_gitignore(existing: str) -> str:
    """统一为最新 BASE_GITIGNORE：公共规则在前，用户自定义规则在后。"""
    known = set(_BASE_GITIGNORE_RULES) | set(_OBSOLETE_GITIGNORE_RULES)
    user_lines = []
    for line in existing.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith(_CILI_IGNORE_PREFIXES) and text not in _LOCAL_ONLY_CILI:
            continue
        if text in known or text in _BASE_GITIGNORE_COMMENTS:
            continue
        user_lines.append(line)
    desired = BASE_GITIGNORE + ("" if not user_lines else "\n".join(user_lines) + "\n")
    return existing if desired == existing else desired


def ensure_shared_gitignore(git_root: str | Path, slug: str) -> bool:
    """把仓库 main 的 .gitignore 调整为「共享 .cili/memory、会话仍本地」。"""
    path = repo_path(git_root, slug)
    if not path.is_dir():
        return False
    git_dir = ["--git-dir", str(path)]
    try:
        head = _run_git([*git_dir, "rev-parse", "--verify", "refs/heads/main"]).strip()
    except RuntimeError:
        return False
    try:
        existing = _run_git(
            [*git_dir, "cat-file", "blob", "main:.gitignore"]
        ).decode("utf-8", "replace")
    except RuntimeError:
        existing = ""
    desired = _merge_shared_gitignore(existing)
    if desired == existing:
        return False

    env = _identity_env()
    blob = _run_git(
        [*git_dir, "hash-object", "-w", "--stdin"],
        input_bytes=desired.encode("utf-8"),
        env=env,
    ).strip()
    index_file = path / "cili-migrate.index"
    index_env = {**env, "GIT_INDEX_FILE": str(index_file)}
    try:
        _run_git([*git_dir, "read-tree", "refs/heads/main"], env=index_env)
        _run_git(
            [
                *git_dir, "update-index", "--add", "--cacheinfo",
                f"100644,{blob.decode()},.gitignore",
            ],
            env=index_env,
        )
        tree = _run_git([*git_dir, "write-tree"], env=index_env).strip()
        commit = _run_git(
            [
                *git_dir, "commit-tree", tree.decode(), "-p", head.decode(),
                "-m", "chore: 共享 .cili，补充常见临时文件忽略",
            ],
            env=env,
        ).strip()
        _run_git(
            [*git_dir, "update-ref", "refs/heads/main", commit.decode(), head.decode()]
        )
    finally:
        index_file.unlink(missing_ok=True)
    return True


def ensure_shared_gitignores(conn: sqlite3.Connection, git_root: str | Path) -> list[str]:
    updated: list[str] = []
    for row in conn.execute("SELECT slug FROM repos"):
        slug = str(row["slug"])
        try:
            if ensure_shared_gitignore(git_root, slug):
                updated.append(slug)
        except Exception:  # noqa: BLE001
            logger.warning("[repos] 更新 %s 的 .gitignore 失败", slug, exc_info=True)
    return updated


def _init_bare_repo(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise ValueError("仓库目录已存在")
    _run_git(["init", "--bare", str(path)])
    _run_git(["--git-dir", str(path), "config", "http.receivepack", "true"])

    env = _identity_env()
    blob = _run_git(
        ["--git-dir", str(path), "hash-object", "-w", "--stdin"],
        input_bytes=BASE_GITIGNORE.encode("utf-8"),
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
    group_id: str = "",
    now: int | None = None,
) -> sqlite3.Row:
    slug = str(slug or "").strip()
    path = repo_path(git_root, slug)
    org = conn.execute(
        "SELECT id FROM organizations WHERE id = ?", (org_id,)
    ).fetchone()
    if org is None:
        raise ValueError("组织不存在")
    owner_group = _validated_group(conn, org_id, group_id)
    _init_bare_repo(path)
    repo_id = "repo_" + uuid.uuid4().hex[:12]
    try:
        conn.execute(
            "INSERT INTO repos (id, org_id, slug, name, group_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                repo_id, org_id, slug, str(name), owner_group,
                int(now if now is not None else time.time()),
            ),
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        shutil.rmtree(path, ignore_errors=True)
        raise
    bump_config_version(conn, org_id)
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
        bump_config_version(conn, str(repo["org_id"]))
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
    bump_config_version(conn, str(repo["org_id"]))
    return True


def _validated_group(conn: sqlite3.Connection, org_id: str, group_id: str):
    value = str(group_id or "").strip()
    if not value:
        return None
    group = conn.execute(
        "SELECT org_id FROM groups WHERE id = ?", (value,)
    ).fetchone()
    if group is None or str(group["org_id"]) != str(org_id):
        raise ValueError("群组不存在或不属于同一组织")
    return value


def set_repo_group(
    conn: sqlite3.Connection, *, repo_id: str, group_id: str = ""
) -> sqlite3.Row:
    """设置仓库所属群组（一对一）；空值表示取消绑定。"""
    repo = get_repo(conn, repo_id)
    if repo is None:
        raise ValueError("仓库不存在")
    owner_group = _validated_group(conn, str(repo["org_id"]), group_id)
    conn.execute(
        "UPDATE repos SET group_id = ? WHERE id = ?", (owner_group, repo_id)
    )
    conn.commit()
    bump_config_version(conn, str(repo["org_id"]))
    return get_repo(conn, repo_id)


def repo_group(conn: sqlite3.Connection, repo_id: str) -> dict | None:
    row = conn.execute(
        "SELECT g.id, g.name FROM repos r"
        " JOIN groups g ON g.id = r.group_id WHERE r.id = ?",
        (repo_id,),
    ).fetchone()
    if row is None:
        return None
    return {"id": str(row["id"]), "name": str(row["name"])}


def repo_access(
    conn: sqlite3.Connection, *, person_id: str, repo_id: str
) -> str | None:
    """仓库授权：人员属于仓库所属群组即可读写。"""
    row = conn.execute(
        "SELECT 1 FROM repos r"
        " JOIN group_members gm ON gm.group_id = r.group_id"
        " WHERE r.id = ? AND gm.person_id = ? LIMIT 1",
        (repo_id, person_id),
    ).fetchone()
    return "write" if row is not None else None
