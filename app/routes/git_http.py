"""Git Smart HTTP 网关：Basic 设备令牌 + 群组 ACL + git http-backend。"""

from __future__ import annotations

import binascii
import base64
import os
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.responses import StreamingResponse

from app.services.audit import record as record_audit
from app.services.identity import authenticate_token
from app.services.repos import get_repo_by_slug, repo_access, repo_path

router = APIRouter(tags=["git"])

MAX_BODY_BYTES = 512 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 120
_REALM = 'Basic realm="cili-org"'


def _basic_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        return ""
    try:
        raw = base64.b64decode(header[6:].strip()).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return ""
    _username, _, password = raw.partition(":")
    return password.strip()


def _unauthorized() -> Response:
    return Response(
        status_code=401,
        headers={"WWW-Authenticate": _REALM},
        content=b"",
    )


def _parse_cgi(raw: bytes) -> Response:
    separator = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
    if separator not in raw:
        return Response(status_code=500, content=b"invalid git backend response")
    header_blob, body = raw.split(separator, 1)
    status = 200
    headers: dict[str, str] = {}
    for line in header_blob.splitlines():
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        key = name.decode("latin-1").strip()
        text = value.decode("latin-1").strip()
        if key.lower() == "status":
            try:
                status = int(text.split()[0])
            except (IndexError, ValueError):
                status = 500
        elif key.lower() not in {"content-length", "transfer-encoding"}:
            headers[key] = text
    return Response(content=body, status_code=status, headers=headers)


def _audit_git(
    request: Request, device, repo, action: str, status: int, size: int, elapsed_ms: int
) -> None:
    record_audit(
        request.app.state.conn,
        org_id=device.org_id,
        actor=device.person_name or device.person_id,
        action=action,
        target=str(repo["slug"]),
        detail=f"status={status} bytes={size} duration_ms={elapsed_ms}",
        ip=request.client.host if request.client else "",
    )


async def _serve(request: Request, repo_name: str, path_info: str, action: str) -> Response:
    token = _basic_token(request)
    device = authenticate_token(request.app.state.conn, token)
    if device is None:
        return _unauthorized()

    if not repo_name.endswith(".git"):
        return Response(status_code=404, content=b"not found")
    slug = repo_name[:-4]
    repo = get_repo_by_slug(request.app.state.conn, slug)
    if repo is None or str(repo["org_id"]) != device.org_id:
        return Response(status_code=404, content=b"not found")

    access = repo_access(
        request.app.state.conn, person_id=device.person_id, repo_id=str(repo["id"])
    )
    if access is None:
        return Response(status_code=403, content=b"forbidden")
    if action == "receive" and access != "write":
        return Response(status_code=403, content=b"read-only")
    if (
        action == "info_refs"
        and request.query_params.get("service") == "git-receive-pack"
        and access != "write"
    ):
        return Response(status_code=403, content=b"read-only")

    content_length = request.headers.get("content-length", "")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                return Response(status_code=413, content=b"payload too large")
        except ValueError:
            return Response(status_code=400, content=b"invalid content-length")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        return Response(status_code=413, content=b"payload too large")

    if str(repo["backend"]) == "forward":
        return await _forward(
            request, device, repo, slug, path_info, action, body
        )

    git_root = Path(request.app.state.settings.git_root)
    path = repo_path(git_root, slug)
    if not path.is_dir():
        return Response(status_code=404, content=b"repository missing")

    env = os.environ.copy()
    env.update(
        {
            "GIT_PROJECT_ROOT": str(git_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "GIT_PROTOCOL": request.headers.get("git-protocol", ""),
            "PATH_INFO": f"/{slug}.git{path_info}",
            "QUERY_STRING": request.url.query,
            "REQUEST_METHOD": request.method,
            "CONTENT_TYPE": request.headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": device.person_name or device.person_id,
            "REMOTE_ADDR": request.client.host if request.client else "",
            "SERVER_PROTOCOL": "HTTP/1.1",
        }
    )

    started = time.monotonic()
    try:
        completed = subprocess.run(
            ["git", "http-backend"],
            input=body,
            env=env,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return Response(status_code=504, content=b"git backend timeout")
    except OSError:
        return Response(status_code=500, content=b"git backend unavailable")

    elapsed_ms = int((time.monotonic() - started) * 1000)
    if completed.returncode != 0:
        _audit_git(request, device, repo, "git_error", 500, len(completed.stderr), elapsed_ms)
        return Response(status_code=500, content=b"git backend error")

    response = _parse_cgi(completed.stdout)
    audit_action = {
        "info_refs": "git_info_refs",
        "upload": "git_upload_pack",
        "receive": "git_receive_pack",
    }.get(action, "git_request")
    _audit_git(
        request, device, repo, audit_action,
        response.status_code, len(response.body), elapsed_ms,
    )
    return response


async def _forward(
    request: Request,
    device,
    repo,
    slug: str,
    path_info: str,
    action: str,
    body: bytes,
) -> Response:
    upstream = str(repo["upstream_url"] or "").strip().rstrip("/")
    if not upstream.startswith(("http://", "https://")):
        return Response(status_code=500, content=b"invalid upstream")

    url = upstream + path_info
    if request.url.query:
        url += "?" + request.url.query

    headers: dict[str, str] = {}
    for name in ("content-type", "git-protocol", "accept", "accept-encoding", "user-agent"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    username = str(repo["upstream_username"] or "")
    password = str(repo["upstream_password"] or "")
    if username or password:
        raw = f"{username or 'org'}:{password}".encode("utf-8")
        headers["authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")

    started = time.monotonic()
    client = request.app.state.git_client
    try:
        upstream_request = client.build_request(
            request.method,
            url,
            headers=headers,
            content=body if body else None,
        )
        upstream_response = await client.send(upstream_request, stream=True)
    except Exception as exc:  # noqa: BLE001
        _audit_git(request, device, repo, "git_error", 502, 0, 0)
        return Response(status_code=502, content=b"upstream unavailable")

    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in {"content-length", "transfer-encoding", "connection"}
    }
    audit_action = {
        "info_refs": "git_info_refs",
        "upload": "git_upload_pack",
        "receive": "git_receive_pack",
    }.get(action, "git_request")

    async def stream():
        total = 0
        try:
            async for chunk in upstream_response.aiter_raw():
                total += len(chunk)
                yield chunk
        finally:
            await upstream_response.aclose()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            _audit_git(
                request, device, repo, audit_action,
                upstream_response.status_code, total, elapsed_ms,
            )

    return StreamingResponse(
        stream(),
        status_code=upstream_response.status_code,
        headers=response_headers,
    )


@router.get("/git/{repo_name}/info/refs")
async def git_info_refs(repo_name: str, request: Request):
    return await _serve(request, repo_name, "/info/refs", "info_refs")


@router.post("/git/{repo_name}/git-upload-pack")
async def git_upload_pack(repo_name: str, request: Request):
    return await _serve(request, repo_name, "/git-upload-pack", "upload")


@router.post("/git/{repo_name}/git-receive-pack")
async def git_receive_pack(repo_name: str, request: Request):
    return await _serve(request, repo_name, "/git-receive-pack", "receive")
