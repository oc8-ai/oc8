"""The wire contract, pinned.

Every field on the agent-version API is camelCase on the wire and snake_case
in Python -- `CamelModel`'s `alias_generator=to_camel` does that, and this file
asserts it for the four names a hand-written client would get wrong
(`versionNo`, `isCurrent`, `publishedAt`, `changedFields`). It also pins the
one field that is REQUIRED rather than optional, which is the whole of the
optimistic-concurrency check.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from oc8.schemas.dto import (
    AgentDraftStatusDTO,
    AgentVersionDiffDTO,
    AgentVersionDTO,
    AgentVersionFieldDiffDTO,
    AgentVersionSummaryDTO,
)
from oc8.schemas.requests import PublishAgentVersionRequest


def test_a_version_summary_serialises_camel_case() -> None:
    dto = AgentVersionSummaryDTO(
        id="11111111-1111-1111-1111-111111111111",
        version_no=3,
        note="tightened the odoo threshold",
        published_by="22222222-2222-2222-2222-222222222222",
        published_at="2026-09-27T10:00:00+00:00",
        is_current=True,
        rolled_back_from=None,
    )
    body = dto.model_dump(by_alias=True)
    assert body["versionNo"] == 3
    assert body["isCurrent"] is True
    assert body["publishedAt"] == "2026-09-27T10:00:00+00:00"
    assert body["publishedBy"] == "22222222-2222-2222-2222-222222222222"
    assert body["rolledBackFrom"] is None


def test_the_full_version_dto_carries_the_payload_and_a_hex_hash() -> None:
    """`payload_hash` is `bytea` in the database and hex on the wire: JSON has
    no bytes type, and base64 would need the client to know which flavour."""
    dto = AgentVersionDTO(
        id="11111111-1111-1111-1111-111111111111",
        version_no=1,
        note=None,
        published_by=None,
        published_at="2026-09-27T10:00:00+00:00",
        is_current=False,
        rolled_back_from=2,
        payload={"mission": "m"},
        payload_hash="00ff",
    )
    body = dto.model_dump(by_alias=True)
    assert body["payload"] == {"mission": "m"}
    assert body["payloadHash"] == "00ff"
    assert body["rolledBackFrom"] == 2


def test_a_diff_names_both_sides_and_allows_a_null_to_version() -> None:
    """`to` defaults to the working copy (spec §4), and the working copy has no
    version number -- so `toVersionNo` is nullable rather than being faked with
    the current number, which would claim the diff was between two published
    versions when it was not."""
    dto = AgentVersionDiffDTO(
        from_version_no=1,
        to_version_no=None,
        entries=[
            AgentVersionFieldDiffDTO(field="mission", before="a", after="b"),
        ],
    )
    body = dto.model_dump(by_alias=True)
    assert body["fromVersionNo"] == 1
    assert body["toVersionNo"] is None
    assert body["entries"][0] == {"field": "mission", "before": "a", "after": "b"}


def test_draft_status_camel_cases_changed_fields() -> None:
    dto = AgentDraftStatusDTO(
        dirty=True, changed_fields=["mission", "narrowing.odoo"], current_version_no=4
    )
    body = dto.model_dump(by_alias=True)
    assert body["changedFields"] == ["mission", "narrowing.odoo"]
    assert body["currentVersionNo"] == 4


def test_publish_requires_the_expected_current_version_number() -> None:
    """Required, not optional, and that is the optimistic check.

    An optional field would let a client omit it and silently lose the
    protection -- which is worse than not having it, because the UI would look
    like it had it. Spec §4's body shape marks only `note` with a `?`.
    """
    with pytest.raises(ValidationError):
        PublishAgentVersionRequest.model_validate({"note": "nope"})

    body = PublishAgentVersionRequest.model_validate({"expectedCurrentVersionNo": 4})
    assert body.expected_current_version_no == 4
    assert body.note is None


def test_a_publish_note_is_length_capped() -> None:
    """A note is a sentence for a human, not a place to stash a payload. Capped
    here rather than in the database so the refusal is a 422 naming the field."""
    with pytest.raises(ValidationError):
        PublishAgentVersionRequest.model_validate(
            {"expectedCurrentVersionNo": 1, "note": "x" * 501}
        )
