"""Community-owned licensing contracts.

The Community package provides neutral entitlement types but never imports an
Enterprise verifier or makes Enterprise licensing decisions.
"""

from oc8.licensing.capabilities import ALL_CAPABILITIES
from oc8.licensing.contracts import CommunityEntitlements, Entitlements
from oc8.licensing.resolver import (
    EntitlementsResolver,
    get_entitlements_resolver,
    set_entitlements_resolver,
)

__all__ = [
    "ALL_CAPABILITIES",
    "CommunityEntitlements",
    "Entitlements",
    "EntitlementsResolver",
    "get_entitlements_resolver",
    "set_entitlements_resolver",
]
