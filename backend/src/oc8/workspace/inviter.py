"""Who sends member invitations. Self-hosted oc8 invites with a local
set-password link (the default, no inviter registered). A deployment whose
people sign in through an external identity service registers an inviter at
startup; `POST /members` and member deletion then go through it, and it may
refuse an invitation. A refusal's `extra` may carry `actionLabel` and
`actionUrl` (a path in the app) that the members screen offers next to the
message."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from oc8.auth import Principal


@dataclass(frozen=True)
class InviteResult:
    #: The token `sub` this person will sign in with.
    subject: str


@dataclass
class InviteRefused(Exception):
    status_code: int
    code: str
    message: str
    extra: dict = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class MemberInviter(Protocol):
    async def invite(
        self, *, tenant_id: uuid.UUID, email: str, role: str, display_name: str, invited_by: Principal
    ) -> InviteResult: ...

    async def remove(self, *, tenant_id: uuid.UUID, subject: str) -> None: ...


_inviter: MemberInviter | None = None


def set_member_inviter(inviter: MemberInviter | None) -> None:
    global _inviter
    _inviter = inviter


def get_member_inviter() -> MemberInviter | None:
    return _inviter
