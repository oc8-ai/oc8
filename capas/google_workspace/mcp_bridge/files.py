"""Files across the tool boundary, the bridge's half (oc8's manifest reference,
"Files in and out of a tool").

A tool returns a file as `FileContent`; `__main__.py` sends it as MCP binary
content, which core keeps as a file of the run. A tool takes a file through a
parameter marked `x-oc8-file`; core resolves the run file id the model passed
there to `{file_id, name, mime_type, size, content_base64}` before the call
arrives, so `attachment_files` only ever decodes what core handed over.
"""

from __future__ import annotations

import base64
import binascii
import mimetypes
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: Gmail takes a whole message, attachments included, up to 25 MB.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

_MEDIA_TYPE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}"
)
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\x7f/\\]+")


@dataclass(frozen=True)
class FileContent:
    """A tool result that is a file; the model reads `details` and core's
    reference to the file, never the bytes."""

    name: str
    mime_type: str
    data: bytes
    details: dict[str, Any]


#: The `attachments` parameter of a tool that sends mail.
ATTACHMENTS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "description": (
        "Files to attach, at most 25 MB together: each is {file} with the id of a file "
        "of this run (from gmail_get_attachment or write_output_file); `name` and "
        "`type` override the file's."
    ),
    "items": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "A run file's id.", "x-oc8-file": True},
            "name": {"type": "string"},
            "type": {"type": "string"},
        },
        "required": ["file"],
    },
}


def attachment_files(items: Sequence[Any] | None) -> list[tuple[str, str, bytes]]:
    """`(name, media type, bytes)` for each attachment core resolved."""
    out: list[tuple[str, str, bytes]] = []
    total = 0
    for item in items or []:
        file = item.get("file") if isinstance(item, dict) else None
        if not isinstance(file, dict):
            raise RuntimeError(
                "An attachment's `file` is a run file id that oc8 turns into the file; "
                "this one reached the tool unresolved."
            )
        name = _UNSAFE_NAME.sub("_", str(item.get("name") or file.get("name") or "")).strip()
        if not name:
            raise RuntimeError("Each attachment needs a name.")
        try:
            data = base64.b64decode(str(file.get("content_base64") or ""), validate=True)
        except (binascii.Error, ValueError):
            raise RuntimeError(f"The content of {name!r} is not valid base64.") from None
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise RuntimeError(f"Attachments may be at most 25 MB together; {name!r} goes over.")
        mime = str(item.get("type") or file.get("mime_type") or "")
        if mime and not _MEDIA_TYPE.fullmatch(mime):
            raise RuntimeError(f"The type of {name!r} is not a media type.")
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        out.append((name, mime, data))
    return out
