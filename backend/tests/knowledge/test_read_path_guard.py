"""Forgetting the deleted_at predicate must fail the build, not the review.

`retrieval.py` is the only place that reads `kb_chunk` today, and both
production readers funnel through `retrieve_kb_context` -- but that is a fact
about the code as it stands, not a guarantee about the next read path somebody
adds. A read that forgets the predicate is indistinguishable from never having
deleted anything, and it fails silently: the deletion still LOOKS done
everywhere an operator would check.

So the rule is structural, in the idiom of
`tests/api/test_every_route_is_governed.py`: the model is referenced only inside
a named set of modules, and every read starts at `live_chunks()`.

The check is an IMPORT guard rather than a call-shape guard on purpose. Matching
`select(KbChunk)` would miss `db.get`, `select(KbChunk.content, ...)`, and the
raw SQL that hybrid retrieval will most naturally use.
"""

from __future__ import annotations

import ast
from pathlib import Path

from oc8.knowledge.chunks import live_chunks

SRC = Path(__file__).resolve().parents[2] / "src" / "oc8"

#: Every module allowed to name the model at all. Adding one here is a decision
#: about the read path, which is the point of making it a diff.
ALLOWED = {
    "models/__init__.py",
    "models/knowledge.py",
    "knowledge/chunks.py",
    "knowledge/ingest.py",
    "knowledge/tombstone.py",
    "knowledge/reconcile.py",
    # Projects (kb_id, source_uri) off `live_chunks()` to find what a narrowed
    # selection left out; every read there starts from the live predicate.
    "knowledge/scope_prune.py",
    "knowledge/retrieval.py",
}


def _references_the_model(path: Path) -> bool:
    """AST, not grep: `worker.py` explains the duplicate-KbChunk race it exists
    to prevent in a docstring, and a comment is not a read."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "KbChunk":
            return True
        if isinstance(node, ast.Attribute) and node.attr == "KbChunk":
            return True
    return False


def test_kb_chunk_is_only_read_through_live_chunks() -> None:
    offenders = sorted(
        str(path.relative_to(SRC)) for path in SRC.rglob("*.py") if _references_the_model(path)
    )

    assert set(offenders) <= ALLOWED, (
        f"{sorted(set(offenders) - ALLOWED)} reads kb_chunk directly. Start from "
        "oc8.knowledge.chunks.live_chunks(), which carries the deleted_at "
        "predicate, or add the module to ALLOWED and say why in the diff."
    )
    assert "knowledge/chunks.py" in offenders, "live_chunks() is where the predicate lives"
    # And what it hands back actually carries the predicate, rather than being a
    # bare select() that every caller is trusted to narrow.
    assert "deleted_at IS NULL" in str(live_chunks())
