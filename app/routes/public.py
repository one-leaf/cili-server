"""公开端点。"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/v1/health")
async def health():
    return {"ok": True, "status": "ok", "name": "cili-server", "version": "1"}


@router.get("/api/v1/public/orgs")
async def public_orgs(request: Request):
    """客户端加入组织前的组织列表；id 使用 slug（客户端以 org-<slug> 命名 provider）。"""
    conn = request.app.state.conn
    rows = conn.execute(
        "SELECT slug, name FROM organizations ORDER BY name, id"
    )
    return {
        "orgs": [
            {"id": str(row["slug"]), "name": str(row["name"])} for row in rows
        ]
    }
