"""What core hands a channel, and what a channel hands back (§5.6).

Neutral on purpose. Nothing here names a messenger, knows what a button is, or
assumes a decision can be rendered at all — a channel may be a chat with inline
keyboards, a numbered list answered by reply, or an email with two links. Core
describes the DECISION; the plugin decides how it looks.

The classification field is the one that carries weight. An approval's detail
holds customer names, amounts and sometimes retrieved knowledge, and a messenger
is a third party in a jurisdiction oc8 does not choose. A channel declares the
highest classification it may carry; above that line `redacted()` produces the
same notice with the content removed and a sentence saying where to read it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field, replace

#: public < internal < confidential < restricted, the scale the knowledge base
#: and the PDP already use (`authz.pdp`, `models.knowledge`). Named here so a
#: channel's ceiling is comparable with the material it is asked to carry.
CLASSIFICATIONS: tuple[str, ...] = ("public", "internal", "confidential", "restricted")


def outranks(material: str, ceiling: str) -> bool:
    """True when `material` is too sensitive for a channel limited to `ceiling`.

    An unknown value on either side counts as too sensitive. Failing closed is
    the only safe reading: the alternative is that a typo in a channel's config
    silently sends customer data to a chat.
    """
    if material not in CLASSIFICATIONS or ceiling not in CLASSIFICATIONS:
        return True
    return CLASSIFICATIONS.index(material) > CLASSIFICATIONS.index(ceiling)


@dataclass(frozen=True)
class NoticeOption:
    """One answer the approver may pick. `key` is what comes back."""

    key: str
    label: str


@dataclass(frozen=True)
class ApprovalNotice:
    approval_id: uuid.UUID
    tenant_id: uuid.UUID
    title: str
    detail: str
    #: Free text, because "value" is not a number in every domain -- a contract
    #: change has no amount and still matters. Empty when there is none.
    amount_text: str = ""
    options: tuple[NoticeOption, ...] = ()
    expires_at: dt.datetime | None = None
    #: The classification of the material in `detail`, decided by core.
    classification: str = "internal"
    #: True once `redacted()` has taken the content out, so a channel can say
    #: "open oc8" rather than pretending the message is complete.
    content_withheld: bool = False
    #: Deep link to the record this request is about, or empty (§6). Empty --
    #: not None -- to match every other optional text field on this frozen
    #: dataclass, so a renderer only ever has to test truthiness.
    record_url: str = ""

    def redacted(self) -> ApprovalNotice:
        """The same request, announced without saying what it is about.

        Used when the material outranks what a channel may carry. Deliberately
        still SENT: the approver learning that something waits is the whole
        value of the channel, and it is also the only version that can be sent
        outside WhatsApp's 24-hour window, where nothing but a template may go.
        """
        return replace(
            self,
            detail="",
            amount_text="",
            options=(),
            content_withheld=True,
            # The URL names the system, the entity and the record id -- the same
            # disclosure the buttons are withheld for.
            record_url="",
        )


@dataclass(frozen=True)
class ChannelDecision:
    """What a channel resolved an inbound message to."""

    approval_id: uuid.UUID
    verdict: str  # "approve" | "reject"
    option_key: str | None = None
    reason: str | None = None
    #: The far platform's account id, resolved to an oc8 user by the binding.
    external_id: str = ""


@dataclass(frozen=True)
class ChannelLink:
    """Somebody sent the bot a binding code. Not yet a permission -- core still
    checks the code is live before it means anything."""

    code: str
    external_id: str


@dataclass(frozen=True)
class ChannelFreeText:
    """Somebody sent the bot an ordinary message -- not a link code, not a
    decision button tap. Authorization and routing happen entirely in
    oc8.channels.dispatch; this module only describes what was received."""

    text: str
    external_id: str


@dataclass(frozen=True)
class ChannelCapabilities:
    """What a channel can and may do, declared by its plugin.

    `max_classification` defaults to `public`, the most restrictive value on the
    scale -- which means a fresh channel carries no content at all until an
    operator widens it, since approvals are `internal` by default. That is the
    intended friction: the failure mode of the convenient default is customer
    data in a chat nobody meant to send it to, and nobody notices that until it
    matters.

    `unsolicited` is false for a channel that may only reply inside a window the
    user opened (WhatsApp's 24 hours). It is not a limitation to work around --
    it decides whether an escalation reminder at 3 a.m. can carry content at all.
    """

    max_classification: str = "public"
    unsolicited: bool = True
    supports_options: bool = True
    metadata: dict[str, str] = field(default_factory=dict)
