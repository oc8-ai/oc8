"""Row <-> JSON encoding for the backup archive (design doc §3, "Column
encoding"). UUID -> str, bytea -> base64, timestamps -> ISO-8601 with offset,
JSONB verbatim, pgvector embedding -> base64 of the raw float32 array.

Encoding is driven off the SQLAlchemy column TYPE, never off the runtime
type of the value: a `str` UUID column and a `str` text column must be told
apart even when both happen to hold a string, and a NULL value must round
-trip as NULL regardless of what type the column is.
"""

from __future__ import annotations

import base64
import datetime as dt
import struct
import uuid
from collections.abc import Mapping
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, LargeBinary, Table
from sqlalchemy import Uuid as SqlaUuid


def _encode_value(value: Any, column_type: Any) -> Any:
    if value is None:
        return None
    if isinstance(column_type, Vector):
        # Raw float32 bytes, not a JSON list: same bits, ~1/3 the size, no
        # decimal round-tripping through text. Little-endian is PINNED rather
        # than native: an archive exists to move a company to another machine,
        # so an encoding that silently depends on the writer's byte order is a
        # portability bug waiting for the first big-endian host. `array("f")`
        # offers no way to say this; `struct` does.
        return base64.b64encode(struct.pack(f"<{len(value)}f", *value)).decode("ascii")
    if isinstance(column_type, SqlaUuid):
        return str(value)
    if isinstance(column_type, LargeBinary):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(column_type, DateTime):
        return value.isoformat()
    # JSONB, ARRAY(Text), Text, Boolean, Integer, BigInteger, ... are already
    # JSON-safe as-is.
    return value


def _decode_value(value: Any, column_type: Any) -> Any:
    if value is None:
        return None
    if isinstance(column_type, Vector):
        raw = base64.b64decode(value)
        return list(struct.unpack(f"<{len(raw) // 4}f", raw))
    if isinstance(column_type, SqlaUuid):
        return uuid.UUID(value)
    if isinstance(column_type, LargeBinary):
        return base64.b64decode(value)
    if isinstance(column_type, DateTime):
        # `fromisoformat` keeps the offset -> a timezone-aware datetime,
        # matching `DateTime(timezone=True)` used throughout this schema.
        return dt.datetime.fromisoformat(value)
    return value


def _data_columns(table: Table) -> list[Column[Any]]:
    """Every column but the generated ones (`kb_chunk.search_tsv`): Postgres
    computes those from the row and refuses an INSERT that sets them, so they
    are neither archived nor restored."""
    return [col for col in table.columns if col.computed is None]


def serialize_row(table: Table, row: Mapping[str, Any]) -> dict[str, Any]:
    """Encode one DB row into JSON-safe values for the archive."""
    return {col.name: _encode_value(row[col.name], col.type) for col in _data_columns(table)}


def deserialize_row(table: Table, data: Mapping[str, Any]) -> dict[str, Any]:
    """Decode one archived row back into native Python/DB-ready values."""
    return {col.name: _decode_value(data[col.name], col.type) for col in _data_columns(table)}
