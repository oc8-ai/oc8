"""Drive tools, scoped to Shared Drives (the RAG connector's own domain, §3 --
these tools are the read/write counterpart). Auth is the service account's OWN
identity (google_api.self_token), never a delegated mailbox: Shared Drive
membership needs no domain-wide delegation."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.types import Tool

from .. import google_api

# Drive's upload endpoint lives at a different path prefix than every other
# Drive call (`/upload/drive/v3/...` vs `/drive/v3/...`) -- computed once here
# rather than re-deriving it (and re-tripping the E501 line limit) at each of
# the two call sites that need it.
API_DRIVE_UPLOAD = google_api.API_DRIVE.replace("/drive/v3", "/upload/drive/v3")


async def drive_list(args: dict[str, Any]) -> Any:
    """Lists every non-trashed file in the Shared Drive, paginating through
    `nextPageToken` until Drive reports none left -- unlike a single-page
    request (Drive's own default page size), this never silently truncates a
    large Shared Drive's listing."""
    token = google_api.self_token()
    files: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, Any] = {
            "q": "trashed = false",
            "corpora": "drive",
            "driveId": args["driveId"],
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "pageSize": 1000,
            "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,webViewLink)",
        }
        if page_token:
            params["pageToken"] = page_token
        page = await google_api.get_json(google_api.API_DRIVE, "/files", token=token, params=params)
        files.extend(page.get("files", []))
        page_token = page.get("nextPageToken", "")
        if not page_token:
            break
    return {"files": files}


async def drive_get_content(args: dict[str, Any]) -> Any:
    token = google_api.self_token()
    raw = await google_api.get_bytes(
        google_api.API_DRIVE,
        f"/files/{args['fileId']}",
        token=token,
        params={"alt": "media", "supportsAllDrives": "true"},
    )
    return raw.decode(errors="replace")


async def drive_search(args: dict[str, Any]) -> Any:
    token = google_api.self_token()
    safe_query = args["query"].replace("\\", "\\\\").replace("'", "\\'")
    return await google_api.get_json(
        google_api.API_DRIVE,
        "/files",
        token=token,
        params={
            "corpora": "drive",
            "driveId": args["driveId"],
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
            "q": f"name contains '{safe_query}' and trashed = false",
            "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
        },
    )


async def drive_upload(args: dict[str, Any]) -> Any:
    token = google_api.self_token()
    metadata = {"name": args["name"], "parents": [args["driveId"]]}
    # A random, unpredictable boundary per request -- a static boundary would
    # let model-supplied `content` (untrusted, LLM-generated) inject an extra
    # MIME part into the request body by embedding the boundary sequence
    # itself. As defense in depth (belt-and-suspenders, not a substitute for
    # the randomness above) we also reject content that happens to contain
    # the chosen boundary outright, rather than silently building a malformed
    # request.
    boundary = uuid.uuid4().hex
    if boundary in args["content"]:
        raise RuntimeError("drive_upload: content collides with the generated multipart boundary")
    body = (
        f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
        f"{json.dumps(metadata)}\r\n"
        f"--{boundary}\r\nContent-Type: text/plain\r\n\r\n"
        f"{args['content']}\r\n--{boundary}--"
    )
    async with google_api.get_client() as client:
        resp = await client.post(
            f"{API_DRIVE_UPLOAD}/files",
            params={"uploadType": "multipart", "supportsAllDrives": "true"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            content=body.encode(),
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Google API error (HTTP {resp.status_code}): {resp.text[:200]}")
    result: dict[str, Any] = resp.json()
    return result


async def drive_update_content(args: dict[str, Any]) -> Any:
    token = google_api.self_token()
    content = args["content"]
    raw = content.encode() if isinstance(content, str) else content
    async with google_api.get_client() as client:
        resp = await client.patch(
            f"{API_DRIVE_UPLOAD}/files/{args['fileId']}",
            params={"uploadType": "media", "supportsAllDrives": "true"},
            headers={"Authorization": f"Bearer {token}"},
            content=raw,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Google API error (HTTP {resp.status_code}): {resp.text[:200]}")
    result: dict[str, Any] = resp.json()
    return result


async def drive_delete(args: dict[str, Any]) -> Any:
    token = google_api.self_token()
    await google_api.delete(
        google_api.API_DRIVE,
        f"/files/{args['fileId']}",
        token=token,
        params={"supportsAllDrives": "true"},
    )
    return {"deleted": args["fileId"]}


TOOLS: list[Tool] = [
    Tool(
        name="drive_list",
        description="List files in a Shared Drive.",
        inputSchema={
            "type": "object",
            "properties": {"driveId": {"type": "string"}},
            "required": ["driveId"],
        },
    ),
    Tool(
        name="drive_get_content",
        description=(
            "Get a file's raw text content (for text-native files; use "
            "docs_get_text/sheets_read_range/slides_get_text for Google-native formats)."
        ),
        inputSchema={
            "type": "object",
            "properties": {"fileId": {"type": "string"}},
            "required": ["fileId"],
        },
    ),
    Tool(
        name="drive_search",
        description="Search filenames within a Shared Drive.",
        inputSchema={
            "type": "object",
            "properties": {"driveId": {"type": "string"}, "query": {"type": "string"}},
            "required": ["driveId", "query"],
        },
    ),
    Tool(
        name="drive_upload",
        description="Upload a new text file into a Shared Drive.",
        inputSchema={
            "type": "object",
            "properties": {
                "driveId": {"type": "string"},
                "name": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["driveId", "name", "content"],
        },
    ),
    Tool(
        name="drive_update_content",
        description="Replace an existing file's content.",
        inputSchema={
            "type": "object",
            "properties": {"fileId": {"type": "string"}, "content": {"type": "string"}},
            "required": ["fileId", "content"],
        },
    ),
    Tool(
        name="drive_delete",
        description="Delete a file from a Shared Drive.",
        inputSchema={
            "type": "object",
            "properties": {"fileId": {"type": "string"}},
            "required": ["fileId"],
        },
    ),
]

CALL_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
    "drive_list": drive_list,
    "drive_get_content": drive_get_content,
    "drive_search": drive_search,
    "drive_upload": drive_upload,
    "drive_update_content": drive_update_content,
    "drive_delete": drive_delete,
}
