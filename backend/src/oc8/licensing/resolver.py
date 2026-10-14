"""Who decides which capabilities the caller's organisation holds.

A single-organisation deployment has one fixed entitlement set and registers
nothing here. An embedding deployment that serves several organisations may
resolve entitlements per organisation: it registers a resolver at startup, and
`GET /api/v1/governance` reports the caller's capabilities from it. With no
resolver registered the capabilities are unknown, which is reported as such.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from oc8.auth import Principal
from oc8.licensing.contracts import Entitlements

type EntitlementsResolver = Callable[[Principal], Awaitable[Entitlements]]

_resolver: EntitlementsResolver | None = None


def set_entitlements_resolver(resolver: EntitlementsResolver | None) -> None:
    global _resolver
    _resolver = resolver


def get_entitlements_resolver() -> EntitlementsResolver | None:
    return _resolver
