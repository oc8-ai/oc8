from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from typing import Any

from oc8.knowledge.connectors.base import (
    AuthContext,
    BrowseEntry,
    Connector,
    ConnectorError,
    FetchedFile,
    RawDocument,
    SourceItemMeta,
    ValidationResult,
)


class UploadConnector:
    type_id = "upload"
    label = "File upload"
    description = "Uploaded document"
    requires_oauth: str | None = None
    config_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "filename": {"type": "string"},
            "content": {"type": "string"},
            "content_type": {"type": "string"},
        },
        "required": ["filename", "content", "content_type"],
    }

    async def validate(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> ValidationResult:
        if not config.get("content"):
            return ValidationResult(ok=False, error="empty content")
        return ValidationResult(ok=True)

    async def discover(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> list[SourceItemMeta]:
        filename = config.get("filename", "")
        return [SourceItemMeta(uri=f"upload://{filename}", title=filename)]

    async def fetch(
        self,
        config: dict[str, Any],
        cursor: dict[str, Any] | None,
        auth: AuthContext | None = None,
    ) -> AsyncIterator[RawDocument]:
        content = str(config["content"])
        yield RawDocument(
            source_uri=f"upload://{config['filename']}",
            title=str(config["filename"]),
            content=content,
            content_type=str(config["content_type"]),
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
        )

    async def browse(
        self, config: dict[str, Any], path: str, auth: AuthContext | None = None
    ) -> list[BrowseEntry]:
        """An upload source is a single file at its root."""
        if path.strip("/"):
            return []
        content = str(config.get("content", ""))
        filename = str(config.get("filename", ""))
        if not filename:
            return []
        return [
            BrowseEntry(
                id=filename,
                name=filename,
                is_folder=False,
                size=len(content.encode()),
                content_type=str(config.get("content_type", "")) or None,
            )
        ]

    async def fetch_file(
        self,
        config: dict[str, Any],
        file_id: str,
        max_bytes: int,
        auth: AuthContext | None = None,
    ) -> FetchedFile:
        filename = str(config.get("filename", ""))
        if not filename or file_id != filename:
            raise ConnectorError("no such file in this source")
        content = str(config.get("content", ""))
        # len(content) <= bytes, and >= bytes / 4: refuse before encoding.
        if len(content) > max_bytes:
            raise ConnectorError(f"file is too large to pin (limit {max_bytes} bytes)")
        data = content.encode()
        if len(data) > max_bytes:
            raise ConnectorError(f"file is too large to pin (limit {max_bytes} bytes)")
        return FetchedFile(
            name=file_id,
            content_type=str(config.get("content_type", "")) or "text/plain",
            data=data,
            text=content,
        )


_UPLOAD: Connector = UploadConnector()
