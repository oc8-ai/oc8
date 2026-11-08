"""The connector seam: what core is allowed to know about a source.

Core learns a `frozenset[str]` of URIs and one `Attestation` member. It never
reads a connector's vendor config -- `maxFiles`, a bucket, a folder id -- because
the moment it does, the registry's software-neutrality rule is gone and every
new connector needs a core change to be reconcilable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class ConnectorError(Exception):
    """Raised for unknown connector type, invalid config, or a blocked fetch."""


@dataclass
class RawDocument:
    source_uri: str
    title: str
    content: str
    content_type: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationResult:
    ok: bool
    error: str | None = None


@dataclass
class SourceItemMeta:
    uri: str
    title: str


class Attestation(StrEnum):
    """How much a connector is willing to swear about a listing it just made.

    These three are core's entire vocabulary for "what does this source hold",
    and the conclusions core may draw from each are written down in exactly one
    place, `reconcile.py`'s module docstring.
    """

    # This URI set is EVERYTHING the source now holds, enumerated to the end of
    # its pagination. Absence from it is evidence -- the only member that is.
    AUTHORITATIVE = "authoritative"
    # Cannot enumerate everything; the named URIs are gone. Absence from a
    # REMOVALS listing means nothing, and it can never forgive a document it
    # does not mention, because it never claimed to see one.
    REMOVALS = "removals"
    # Conclude nothing. An API error, a timeout, and a connector with no
    # `attest_listing` at all collapse to this.
    NONE = "none"


@dataclass(frozen=True)
class SourceListing:
    """A connector's answer to "what do you hold right now".

    Frozen because core passes it into the one function that may delete on the
    strength of it; a listing that could be edited between the refusals and the
    diff would make those refusals advisory.

    `reason` carries the downgrade in words. A `NONE` an operator cannot explain
    reads as a bug in the sweep, and they go looking in the wrong place.
    """

    attestation: Attestation
    present_uris: frozenset[str] = frozenset()  # meaningful only for AUTHORITATIVE
    removed_uris: frozenset[str] = frozenset()  # meaningful only for REMOVALS
    reason: str | None = None


@runtime_checkable
class AuthContext(Protocol):
    """What a connector uses to authenticate.

    `token()` hands out an OAuth access token that is always fresh -- refresh is
    invisible to connectors. `secret(ref)` reaches a non-OAuth credential (an S3
    access key, say) from the tenant's secret store BY REFERENCE, so the source
    config carries the reference and never the value.
    """

    async def token(self) -> str: ...
    async def secret(self, ref: str) -> str: ...
    async def credential(self, credential_id: str, field_key: str) -> str: ...


@runtime_checkable
class Connector(Protocol):
    """What every source connector must answer.

    `attest_listing` (see `AttestingConnector`) is deliberately NOT on this list.
    It is optional, resolved by `getattr`, and a connector that never grows one
    keeps working and means `Attestation.NONE`.

    The `cursor` handed to `fetch` is **core-owned**. `ingest.py` is its only
    writer in the repo, and this slice makes that ownership explicit rather than
    a coincidence: `cursor["hashes"]` is a core-maintained set of content digests
    a connector MAY consult to skip unchanged documents, and core may REMOVE
    entries from it -- it does, when a document that vanished upstream is
    reduced, so that the same document can be ingested again if it comes back.
    A connector must therefore treat a missing digest as "fetch it", never as
    "this cannot have been seen before".
    """

    type_id: str
    # Presentation is declared by the connector itself.  The core only renders
    # this metadata; it must not keep a catalogue of vendor-specific sources.
    label: str
    description: str
    config_schema: dict[str, Any]
    # Provider id when the connector needs an oauth_connection, else None.
    requires_oauth: str | None

    async def validate(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> ValidationResult: ...
    async def discover(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> list[SourceItemMeta]: ...
    def fetch(
        self,
        config: dict[str, Any],
        cursor: dict[str, Any] | None,
        auth: AuthContext | None = None,
    ) -> AsyncIterator[RawDocument]: ...


class AttestingConnector(Protocol):
    """The OPTIONAL half of the seam: a connector willing to say what it holds.

    Core resolves this with `getattr(connector, "attest_listing", None)` and
    never `isinstance`, exactly as `evidence/sweep.py` resolves the optional
    evidence half of the runtime seam, and for the identical reason: a plugin
    loaded from outside this package only has to answer the name, not import
    core's type to prove it. It is a separate Protocol rather than a member of
    `Connector` so that the four shipped connectors stay structurally valid
    `Connector`s without growing a method they cannot honestly implement --
    declaring it on `Connector` would make the optional thing mandatory. It is
    NOT `@runtime_checkable`, so an `isinstance` against it raises at import of
    the first call rather than quietly answering a question it cannot answer:
    a class can satisfy the name and still be lying, which is why the refusals
    in `reconcile.py` exist.

    `discover()` is not this method and must never be used as one. `discover()`
    answers a PREVIEW question and is allowed to be a sample -- `website.py`
    returns exactly one item, the start URL -- so attaching a completeness claim
    to it would make every preview call a deletion authority.

    An implementation raises rather than attesting an empty set when the API
    refuses it: a revoked grant answering `AUTHORITATIVE` with no URIs is the
    wire shape of "the customer deleted everything".
    """

    async def attest_listing(
        self, config: dict[str, Any], auth: AuthContext | None = None
    ) -> SourceListing: ...


@dataclass(frozen=True)
class BrowseEntry:
    id: str
    name: str
    is_folder: bool
    size: int | None = None
    content_type: str | None = None


@dataclass(frozen=True)
class FetchedFile:
    name: str
    content_type: str
    data: bytes
    #: Extracted text -- what read_context pages through.
    text: str


class BrowsingConnector(Protocol):
    """The OPTIONAL browsing half of the seam (oc8 Chat spec §5.4), resolved by
    `getattr` exactly like `AttestingConnector`: a source without it can still
    be pinned as a whole. Capas implement it; core names no vendor.

    `fetch_file` and not `fetch`: `Connector.fetch` already streams every
    document for ingestion.

    Implementations must treat `path` and `file_id` as untrusted input. Core
    only rejects literal `..` segments and control characters, so a connector
    that decodes ids (URL-encoding, base64, ...) decodes them itself and
    re-checks the DECODED value (this is what catches `%2e%2e`). They must never
    put config values or credentials in an error message.

    `fetch_file` must refuse (raise `ConnectorError`) or stop reading once the
    file exceeds `max_bytes`; core re-checks as defence in depth. Both `data`
    and `text` must be at most `max_bytes` (UTF-8 for `text`). A non-text file
    decodes `text` with `errors="replace"` or raises `ConnectorError`."""

    async def browse(
        self, config: dict[str, Any], path: str, auth: AuthContext | None = None
    ) -> list[BrowseEntry]: ...

    async def fetch_file(
        self,
        config: dict[str, Any],
        file_id: str,
        max_bytes: int,
        auth: AuthContext | None = None,
    ) -> FetchedFile: ...
