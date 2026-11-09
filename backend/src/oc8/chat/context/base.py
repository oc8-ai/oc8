"""What a chat can point at (oc8 Chat spec §5.1), and who may.

Every check here is made with the MEMBER's authority, never the agent's:
pinning is the member's explicit release, so it can only release what the
member could open themselves (§5.5). Each resolver reuses the rule the rest of
oc8 already applies to its object; none of them invents a new one.

What a resolver `read`s is untrusted content handed back as plain data.
Fencing it for the model is the manifest's job, not this module's."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.authority import Authority, authority_for_member
from oc8.authz.scope import DepartmentScope, scope_for_member

PAGE_CHARS = 8000


@dataclass(frozen=True)
class Viewer:
    """The member a ref is resolved for, with what they may do and where."""

    member: m.OrgMember
    authority: Authority
    scope: DepartmentScope


async def viewer_for_member(
    db: AsyncSession, member: m.OrgMember, *, token_role: str | None = None
) -> Viewer:
    """`token_role` MUST be the role on the live, authenticated request token
    of THIS member -- never a role read back from storage, and never another
    person's (an agent's, a run's operator's, a sharer's). It is the one term
    of a member's authority that is not in the database, so whatever is passed
    here is believed; lending a role widens the viewer to it. Without it a
    member with no assigned role holds only their stored rows, which is the
    fail-closed direction."""
    return Viewer(
        member=member,
        authority=await authority_for_member(db, member, token_role=token_role),
        scope=await scope_for_member(db, member, token_role=token_role),
    )


@dataclass(frozen=True)
class ContextPage:
    text: str
    page: int
    pages: int


def paginate(text: str, page: int) -> ContextPage:
    """Page `page` (1-based, clamped into range) of `text` in `PAGE_CHARS` slices.
    Empty text is one empty page."""
    pages = max(1, math.ceil(len(text) / PAGE_CHARS))
    current = min(max(1, page), pages)
    start = (current - 1) * PAGE_CHARS
    return ContextPage(text=text[start : start + PAGE_CHARS], page=current, pages=pages)


class ContextResolver(Protocol):
    """One kind of thing a chat can point at.

    The registry asks, in order, and drops the ref on the first refusal so
    every refusal reads the same to the member:

    * `admits` -- a viewer-only gate, checked BEFORE anything is loaded, so a
      viewer who may see nothing of this kind cannot time a load to learn
      whether an id exists. A kind with no such gate returns True.
    * `load` -- the live object of `tenant_id`, or None (missing, deleted,
      another tenant's, malformed id).
    * `authorize` -- the per-object answer for the viewer.

    `ref_id(obj)` is the canonical id of a loaded object; the registry reports
    and de-duplicates on it, never on the id it was handed.
    `classification` is the object's sensitivity label (`public` ..
    `restricted`), which may need the database to work out."""

    kind: str
    readable: bool

    def admits(self, viewer: Viewer) -> bool: ...

    async def load(self, db: AsyncSession, tenant_id: Any, ref_id: str) -> Any | None: ...

    async def authorize(self, db: AsyncSession, viewer: Viewer, obj: Any) -> bool: ...

    def ref_id(self, obj: Any) -> str: ...

    def label(self, obj: Any) -> str: ...

    async def classification(self, db: AsyncSession, obj: Any) -> str: ...

    async def summary(self, db: AsyncSession, obj: Any) -> str: ...

    def deep_link(self, obj: Any) -> str | None: ...

    async def read(self, db: AsyncSession, obj: Any, page: int) -> ContextPage | None: ...

    async def search(
        self, db: AsyncSession, viewer: Viewer, query: str, limit: int
    ) -> list[Any]: ...
