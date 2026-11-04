"""Which knowledge content may be embedded by a cloud model.

Chat never sends `restricted` material to a non-local model
(`ModelRouter._guard_and_resolve`'s ClassificationViolation, and
`classification_rule` at retrieval). An embedding sends the same text to the
same kind of provider, so it follows the same line: content labelled
`restricted` -- by its base, its source or the chunk itself -- or held in a
local-only base is embedded locally or not at all. The reason returned here is
passed to `ModelRouter.embed(require_local=...)`, which refuses any cloud
provider with it.
"""

from __future__ import annotations

from oc8 import models as m

#: Least to most sensitive -- the order of `ck_source_classification`.
CLASSIFICATIONS = ("public", "internal", "confidential", "restricted")


def strictest(*labels: str | None) -> str:
    """The most sensitive of `labels`; an unknown label counts as `restricted`
    (an unrecognised label is no reason to loosen anything)."""
    rank = 0
    for label in labels:
        if label is None:
            continue
        rank = max(
            rank,
            CLASSIFICATIONS.index(label) if label in CLASSIFICATIONS else len(CLASSIFICATIONS) - 1,
        )
    return CLASSIFICATIONS[rank]


def require_local_reason(
    *, kb: m.KnowledgeBase | None = None, classification: str | None = None
) -> str | None:
    """Why this content may only be embedded locally, or None."""
    if kb is not None and kb.local_only:
        return "the knowledge base is local-only"
    label = strictest(kb.classification if kb is not None else None, classification)
    if label == "restricted":
        return "restricted classification requires a local model"
    return None
