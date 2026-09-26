"""Process-wide catalogue of what loaded plugins contribute (§13).

**Read this before adding anything here.** A Python module import is
process-global, but a plugin install is per tenant. This project has already
shipped that mismatch once as a Critical: ``get_hook_registry()`` was a
process-global singleton, and once hooks were dispatched from production call
sites one tenant's plugin handler fired on another tenant's events -- a
cross-tenant data leak *and* a privilege escalation.

So the two halves are deliberately split:

* **Contribution** (this module) is process-wide and keyed by **plugin id**.
  Importing once per process is unavoidable and fine.
* **Resolution** (``oc8.knowledge.connectors.registry.resolve_connector`` and
  anything like it added later) is ALWAYS filtered by the tenant's installed
  *and enabled* plugin set.

This module therefore never answers "which connector serves type X?" -- only
"what did plugin P contribute?". Do not add a flat lookup across all plugins;
that is precisely the shape that caused the earlier defect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from oc8.channels.base import ChannelFactory
    from oc8.knowledge.connectors.base import Connector
    from oc8.knowledge.vector_indexes.base import VectorIndex
    from oc8.modelrouter.registry import ProviderEntry
    from oc8.runtime.adapter import RuntimeAdapter


class PluginContributions:
    """Handed to a plugin's ``register()`` so it can contribute implementations.

    A plugin only ever sees its own instance, so it cannot inspect or overwrite
    another plugin's contributions.
    """

    def __init__(self, plugin_id: str) -> None:
        self.plugin_id = plugin_id
        self.connectors: dict[str, Connector] = {}
        self.vector_indexes: dict[str, VectorIndex] = {}
        self.channels: dict[str, ChannelFactory] = {}
        self.runtime: type[RuntimeAdapter] | None = None
        self.providers: dict[str, ProviderEntry] = {}
        # point -> handler callable. Registration additionally requires the
        # point to be declared in the manifest's `handles`; see lifecycle.
        self.hooks: dict[str, Callable[..., object]] = {}

    def add_connector(self, connector: Connector) -> None:
        type_id = getattr(connector, "type_id", "")
        if not type_id:
            raise ValueError("a contributed connector must have a non-empty type_id")
        self.connectors[type_id] = connector

    def add_vector_index(self, index: VectorIndex) -> None:
        """A query-only retrieval backend for an existing remote collection."""
        type_id = getattr(index, "type_id", "")
        if not type_id:
            raise ValueError("a contributed vector index must have a non-empty type_id")
        self.vector_indexes[type_id] = index

    def add_channel(self, channel_id: str, factory: ChannelFactory) -> None:
        """An approval channel: somewhere a human can be asked and can answer.

        A FACTORY rather than an instance, because one process serves every
        tenant and they do not share a bot: the credential and the classification
        ceiling belong to the installation, so core resolves those and asks for a
        channel built from them at the moment it needs one.

        Keyed by `channel_id` because that is what a binding row stores and what
        an inbound webhook is addressed to -- the name has to survive a restart,
        so it belongs to the plugin, not to load order.
        """
        if not channel_id:
            raise ValueError("a contributed approval channel must have a non-empty channel_id")
        if not callable(factory):
            raise ValueError("an approval channel is contributed as a factory")
        self.channels[channel_id] = factory

    def add_runtime(self, runtime: type[RuntimeAdapter]) -> None:
        """A runtime_adapter plugin supplies exactly one implementation -- an
        agent points at one plugin, so a second would be ambiguous."""
        if self.runtime is not None:
            raise ValueError("a plugin may contribute only one runtime")
        self.runtime = runtime

    def add_model_provider(
        self,
        *,
        canonical: str,
        locality: str,
        factory: object,
        available: object,
        aliases: tuple[str, ...] = (),
    ) -> None:
        from oc8.modelrouter.registry import ProviderEntry

        if not canonical:
            raise ValueError("a contributed provider needs a canonical name")
        if locality not in ("cloud", "local"):
            raise ValueError("locality must be 'cloud' or 'local'")
        self.providers[canonical] = ProviderEntry(
            canonical=canonical,
            aliases=tuple(aliases),
            locality=locality,
            factory=factory,  # type: ignore[arg-type]
            available=available,  # type: ignore[arg-type]
        )

    def add_hook(self, point: str, fn: Callable[..., object]) -> None:
        """Offer an in-process handler for a hook point. Offering is not
        registering: the plugin must ALSO declare the point in its manifest
        `handles`, so it cannot hook a point its consent screen never showed."""
        if not point:
            raise ValueError("a hook needs a point")
        if not callable(fn):
            raise ValueError("a hook handler must be callable")
        self.hooks[point] = fn


_catalogue: dict[str, PluginContributions] = {}


def record(contrib: PluginContributions) -> None:
    _catalogue[contrib.plugin_id] = contrib


def connectors_for(plugin_id: str) -> dict[str, Connector]:
    """Connectors contributed by ONE plugin. Callers must already have
    established that the tenant is entitled to this plugin."""
    entry = _catalogue.get(plugin_id)
    return dict(entry.connectors) if entry is not None else {}


def vector_indexes_for(plugin_id: str) -> dict[str, VectorIndex]:
    """Vector indexes contributed by ONE plugin. Entitlement is the caller's
    job, same as ``connectors_for``."""
    entry = _catalogue.get(plugin_id)
    return dict(entry.vector_indexes) if entry is not None else {}


def runtime_for(plugin_id: str) -> type[RuntimeAdapter] | None:
    """The runtime contributed by ONE plugin. The caller must already have
    established that the tenant is entitled to this plugin."""
    entry = _catalogue.get(plugin_id)
    return entry.runtime if entry is not None else None


def providers_for(plugin_id: str) -> dict[str, ProviderEntry]:
    """Model providers contributed by ONE plugin."""
    entry = _catalogue.get(plugin_id)
    return dict(entry.providers) if entry is not None else {}


def channels_for(plugin_id: str) -> dict[str, ChannelFactory]:
    """Approval channels contributed by ONE plugin. Entitlement is the caller's
    job, same as everywhere else in this module."""
    entry = _catalogue.get(plugin_id)
    return dict(entry.channels) if entry is not None else {}


def hooks_for(plugin_id: str) -> dict[str, Callable[..., object]]:
    """Hook handlers OFFERED by one plugin. The caller still has to check the
    manifest declared the point."""
    entry = _catalogue.get(plugin_id)
    return dict(entry.hooks) if entry is not None else {}


def forget(plugin_id: str) -> None:
    _catalogue.pop(plugin_id, None)


def reset_for_tests() -> None:
    _catalogue.clear()
