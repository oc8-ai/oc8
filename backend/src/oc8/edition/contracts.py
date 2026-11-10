"""Narrow, explicit contracts for build-time edition composition."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

from fastapi import APIRouter

if TYPE_CHECKING:
    from oc8.copilot.agent_templates import AgentTemplate
    from oc8.edition.tools import EditionTool


class EditionExtension(Protocol):
    """A trusted edition contribution mounted by the Community app factory.

    Routers, service routers and edition tools -- each surface added only once
    an implemented edition needs it.  It is not the Marketplace plugin contract.
    """

    def routers(self) -> Sequence[APIRouter]:
        """Return operator API routers contributed by this edition."""
        ...

    def service_routers(self) -> Sequence[APIRouter]:
        """Return service-to-service API routers contributed by this edition.

        Unlike routers(), these are mounted WITHOUT get_principal/deny_*
        dependencies -- each router verifies its own service-to-service
        credential (e.g. a static bearer token), the same way
        internal_agent_router verifies its own run-scoped agent token.

        Defaults to no routers so existing extensions (e.g.
        SupervisionExtension) that only contribute operator routers don't
        need to implement it. `Protocol` subclasses inherit this concrete
        body through ordinary nominal inheritance.
        """
        return ()

    def agent_templates(self) -> Sequence[AgentTemplate]:
        """Agent ideas this edition adds to the Copilot's empty chat (Copilot
        Agent Builder §3.6). Appended after core's; a key core already uses is
        ignored. Defaults to none, like tools()."""
        return ()

    def tools(self) -> Sequence[EditionTool]:
        """Edition tools for the agent and chat tool assembly (oc8 Chat §6.2).

        Offered only to tenants whose entitlements hold each tool's
        capability; `create_app` refuses one whose name shadows a core control
        tool. Defaults to none, like service_routers().
        """
        return ()
