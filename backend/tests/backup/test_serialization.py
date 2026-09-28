"""Row <-> JSON encoding round-trips losslessly for every exported table
(design doc §3, "Column encoding"). Driven off `exported_tables()` from Task
1, so a newly added table is covered automatically."""

from __future__ import annotations

import base64
import datetime as dt
import math
import uuid
from array import array
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, Boolean, DateTime, Integer, LargeBinary, Table
from sqlalchemy import Uuid as SqlaUuid
from sqlalchemy.dialects.postgresql import JSONB

from oc8.backup.serialization import deserialize_row, serialize_row
from oc8.backup.tables import exported_tables, table_by_name

_SAMPLE_VECTOR = [0.5, -1.25, 2.0]
_SAMPLE_DATETIME = dt.datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=dt.UTC)
_SAMPLE_JSON: dict[str, Any] = {"k": "v", "n": 1, "nested": {"a": [1, 2, 3]}, "flag": True}


def _sample_value(column_type: Any, index: int) -> Any:
    """A non-NULL sample value appropriate to `column_type`, driven off the
    SQLAlchemy type (not a guess from any existing Python value)."""
    if isinstance(column_type, Vector):
        return list(_SAMPLE_VECTOR)
    if isinstance(column_type, SqlaUuid):
        return uuid.uuid4()
    if isinstance(column_type, LargeBinary):
        return bytes([index % 256, 0, 255, 17])
    if isinstance(column_type, DateTime):
        return _SAMPLE_DATETIME
    if isinstance(column_type, JSONB):
        return dict(_SAMPLE_JSON)
    if isinstance(column_type, ARRAY):
        return ["a", "b", "c"]
    if isinstance(column_type, Boolean):
        return True
    if isinstance(column_type, Integer):
        return 42
    return f"sample-text-{index}"


def _assert_round_trips(table: Table, row: dict[str, Any]) -> None:
    encoded = serialize_row(table, row)
    decoded = deserialize_row(table, encoded)
    for col in table.columns:
        original = row[col.name]
        got = decoded[col.name]
        if original is None:
            assert got is None, f"{table.name}.{col.name}: NULL became {got!r}"
            continue
        if isinstance(col.type, Vector):
            assert len(got) == len(original)
            for got_v, want_v in zip(got, original, strict=True):
                assert math.isclose(got_v, want_v, rel_tol=1e-6, abs_tol=1e-6), (
                    f"{table.name}.{col.name}: {got!r} != {original!r}"
                )
            continue
        assert got == original, f"{table.name}.{col.name}: {got!r} != {original!r}"


def test_uuid_bytea_timestamp_round_trip() -> None:
    table = table_by_name("agent")
    now = dt.datetime.now(dt.UTC)
    row: dict[str, Any] = {c.name: None for c in table.columns}
    row["id"] = uuid.uuid4()
    row["tenant_id"] = uuid.uuid4()
    row["created_at"] = now
    encoded = serialize_row(table, row)
    assert encoded["id"] == str(row["id"])
    assert encoded["created_at"] == now.isoformat()
    decoded = deserialize_row(table, encoded)
    assert decoded["id"] == row["id"]
    assert decoded["created_at"] == now
    assert decoded["created_at"].tzinfo is not None


def test_embedding_round_trips_as_base64_float32() -> None:
    table = table_by_name("kb_chunk")
    values = [0.1, -2.5, 3.75]
    row: dict[str, Any] = {c.name: None for c in table.columns}
    row["embedding"] = values

    encoded = serialize_row(table, row)

    raw = base64.b64decode(encoded["embedding"])
    arr: array[float] = array("f")
    arr.frombytes(raw)
    assert list(arr) == [array("f", [v]).tolist()[0] for v in values]

    decoded = deserialize_row(table, encoded)
    assert len(decoded["embedding"]) == len(values)
    for got, want in zip(decoded["embedding"], values, strict=True):
        assert abs(got - want) < 1e-4


def test_null_round_trips_as_null_for_every_nullable_column() -> None:
    for name in sorted(exported_tables()):
        table = table_by_name(name)
        row = {
            col.name: (None if col.nullable else _sample_value(col.type, i))
            for i, col in enumerate(table.columns)
        }
        _assert_round_trips(table, row)


def test_every_exported_table_round_trips_a_fully_populated_row() -> None:
    tables = exported_tables()
    # guards against a silently-shrunk table list (57 since migration 0086's
    # file_attachment, migration 0088's member_dashboard_layout, migration
    # 0089's dashboard_preset, and migration 0093's api_key joined the export
    # -- see test_tables.py's own comment on why a chat/instruction
    # attachment's row, a member's saved widget-grid arrangements, and a
    # self-issued API key are all portable company data, not instance-bound)
    assert len(tables) == 57
    for name in sorted(tables):
        table = table_by_name(name)
        row = {col.name: _sample_value(col.type, i) for i, col in enumerate(table.columns)}
        _assert_round_trips(table, row)


def test_embeddings_encode_little_endian_regardless_of_host() -> None:
    """The byte order is pinned, not inherited from whoever ran the export.

    An archive exists to move a company to another machine. `array("f")` writes
    native byte order and offers no way to say otherwise, so an export written
    on a big-endian host would decode to garbage on a little-endian one -- and
    silently, as plausible floats. This pins the wire bytes so that a future
    edit back to `array` fails here instead of in a customer's restored
    knowledge base.
    """
    import base64
    import struct

    from pgvector.sqlalchemy import Vector

    from oc8.backup.serialization import _decode_value, _encode_value

    vector_type = Vector(3)
    encoded = _encode_value([1.0, -2.5, 0.0], vector_type)

    assert base64.b64decode(encoded) == struct.pack("<3f", 1.0, -2.5, 0.0)
    assert _decode_value(encoded, vector_type) == [1.0, -2.5, 0.0]
