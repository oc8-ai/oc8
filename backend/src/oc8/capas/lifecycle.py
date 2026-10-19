"""Plugin lifecycle: consent-gated enable, disable, and the failure
circuit-breaker that quarantines a misbehaving plugin (§13.5).

Enabling registers a plugin's declared ``handles`` bindings. Trusted
(first_party/verified) plugins get an ``InProcessExecutor`` bound to the
callable their entry point offered; community plugins keep the sandboxed
executor and never run code in this process.

Enabling also materialises the data a data-only plugin ships (see
``oc8.capas.materialise``) -- idempotently, and never undone by disabling.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.capas.discovery import find_plugin
from oc8.capas.materialise import materialise_plugin_data
from oc8.capas.service import PluginError
from oc8.capas.claude_hooks import register_claude_hooks_for_capa, unregister_claude_hooks_for_capa
from oc8.hooks.executor import InProcessExecutor, SandboxedExecutor
from oc8.hooks.registry import HookRegistry, get_hook_registry
from oc8.hooks.types import HookHandler
from oc8.models import Capa, CapaInstallation, CapaVersion

CIRCUIT_BREAKER_THRESHOLD = 5

logger = logging.getLogger(__name__)


async def _unregister_and_teardown(reg: HookRegistry, capa_id: uuid.UUID | str) -> None:
    """Unregister a plugin's handlers and best-effort tear down any
    sandboxed containers they held. A teardown failure never blocks the
    disable/quarantine from completing (orphaned-container cleanup is
    best-effort, not load-bearing for the state transition)."""
    capa_key = str(capa_id)
    removed = reg.unregister_plugin(capa_key)
    for handler in removed:
        if isinstance(handler.executor, SandboxedExecutor):
            try:
                await handler.executor.teardown()
            except Exception:
                logger.exception(
                    "sandbox teardown failed for plugin %s (point %s)",
                    capa_id,
                    handler.point,
                )


def _register_claude_hooks(
    tenant_id: uuid.UUID,
    plugin: Capa,
    version: CapaVersion,
    granted: list[str],
) -> None:
    manifest = version.manifest or {}
    claude_hooks = manifest.get("claude_hooks")
    if not claude_hooks:
        return
    discovered = find_plugin(plugin.name)
    if discovered is None:
        logger.warning("plugin %s has claude_hooks but is not on disk", plugin.name)
        return
    register_claude_hooks_for_capa(
        tenant_id,
        capa_id=str(plugin.id),
        plugin_name=plugin.name,
        capa_path=discovered.path,
        trust_level=plugin.trust_level,
        granted_permissions=granted,
        claude_hooks=claude_hooks,
    )


def _unregister_claude_hooks(tenant_id: uuid.UUID, capa_id: uuid.UUID | str) -> None:
    unregister_claude_hooks_for_capa(tenant_id, capa_id=str(capa_id))


class ConsentError(PluginError):
    pass


class QuarantinedError(PluginError):
    """Raised when ``enable_plugin`` is called on an installation the
    circuit breaker has quarantined. Quarantine is a deliberate stop; it
    must never be silently reversible by calling enable again with valid
    consent -- that would let a caller bypass the breaker outright. There is
    no un-quarantine endpoint (out of scope here); an operator must clear
    the installation's state through some other explicit path before
    enable_plugin will accept it again."""


async def _installation(
    db: AsyncSession, tenant_id: uuid.UUID, capa_id: uuid.UUID
) -> CapaInstallation:
    inst = (
        await db.execute(select(CapaInstallation).where(CapaInstallation.capa_id == capa_id))
    ).scalar_one_or_none()
    if inst is None:
        inst = CapaInstallation(tenant_id=tenant_id, capa_id=capa_id, status="installed")
        db.add(inst)
        await db.flush()
    return inst


async def _current_version(db: AsyncSession, plugin: Capa) -> CapaVersion | None:
    if plugin.current_version_id is None:
        return None
    return await db.get(CapaVersion, plugin.current_version_id)


def _trusted_handlers(plugin: Capa) -> dict[str, object]:
    """In-process handlers a trusted plugin offers through its entry point.

    A failure to load is not fatal here: the plugin still enables, it simply
    contributes no handlers (the loader logs and quarantines it).
    """
    from oc8.capas.contributions import hooks_for
    from oc8.capas.discovery import find_plugin
    from oc8.capas.loader import load_plugin

    discovered = find_plugin(plugin.name)
    if discovered is None or not load_plugin(discovered):
        return {}
    return dict(hooks_for(plugin.name))


def _register_hooks(
    tenant_id: uuid.UUID, plugin: Capa, version: CapaVersion, granted: list[str]
) -> None:
    """Register the plugin's declared ``handles`` bindings into the hook registry.

    Trusted (first_party/verified) plugins get an ``InProcessExecutor`` bound to
    the callable their entry point offered. This used to be refused outright
    because resolving that callable "requires entry-point loading, a later
    sub-project" -- that loader now exists, so the restriction is gone.

    Community plugins keep the sandboxed path with ``fn=None``. Their code is
    never run in this process. (That path is still gated on the §8.5 sandbox
    worker, which remains a stub -- so a community handler registers but cannot
    yet execute.)

    A point must be BOTH declared in the manifest and offered by the entry
    point. Declaring without offering leaves nothing to run; offering without
    declaring would let a plugin hook a point its consent screen never showed.
    """
    reg = get_hook_registry(tenant_id)
    is_community = plugin.trust_level == "community"
    offered = {} if is_community else _trusted_handlers(plugin)
    executor: object = (
        SandboxedExecutor(tenant_id=tenant_id, grants={str(plugin.id): granted})
        if is_community
        else InProcessExecutor()
    )
    for binding in version.manifest.get("handles", []):
        point = binding["point"] if isinstance(binding, dict) else binding.point
        priority = (
            int(binding.get("priority", 50)) if isinstance(binding, dict) else binding.priority
        )
        replace = (
            bool(binding.get("replace", False)) if isinstance(binding, dict) else binding.replace
        )
        fn = offered.get(point)
        if not is_community and fn is None:
            # Declared but never offered: registering it would give the bus a
            # handler whose executor asserts fn is not None, i.e. a guaranteed
            # failure on every dispatch.
            logger.warning(
                "plugin %s declares hook %r but its entry point offers no handler",
                plugin.name,
                point,
            )
            continue
        reg.register_handler(
            HookHandler(
                plugin_id=str(plugin.id),
                point=point,
                priority=priority,
                replace=replace,
                fn=fn,  # type: ignore[arg-type]
                executor=executor,  # type: ignore[arg-type]
            )
        )


async def enable_plugin(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    capa_id: uuid.UUID,
    granted_permissions: list[str],
) -> CapaInstallation:
    plugin = await db.get(Capa, capa_id)
    if plugin is None:
        raise PluginError("plugin not found")
    version = await _current_version(db, plugin)
    if version is None:
        raise PluginError("plugin has no current version")
    required = set(version.permissions)
    if not required.issubset(set(granted_permissions)):
        raise ConsentError(f"missing consent for: {sorted(required - set(granted_permissions))}")
    inst = await _installation(db, tenant_id, capa_id)
    if inst.status == "quarantined":
        raise QuarantinedError(
            f"plugin {capa_id} installation is quarantined by the circuit "
            "breaker and cannot be silently re-enabled"
        )
    if inst.status == "enabled":
        # Re-enable of an already-enabled installation (e.g. re-consent
        # after a permission change): tear down the prior registration
        # first so enable_plugin is idempotent -- otherwise this would
        # append a second HookHandler for the same plugin_id/point, firing
        # twice per dispatch. Capabilities need no such care: they are read
        # from the installation row per check, so re-enabling cannot
        # double-count and disabling cannot under-count.
        await _unregister_and_teardown(get_hook_registry(tenant_id), capa_id)
        _unregister_claude_hooks(tenant_id, capa_id)
    inst.status = "enabled"
    inst.version_id = version.id
    inst.granted_permissions = list(granted_permissions)
    inst.enabled_at = dt.datetime.now(tz=dt.UTC)
    inst.disabled_reason = None
    inst.failure_count = 0
    await db.flush()
    _register_hooks(tenant_id, plugin, version, list(granted_permissions))
    _register_claude_hooks(tenant_id, plugin, version, list(granted_permissions))
    # Data-only plugin types (skill / flow_template / tool_pack) become real rows
    # here: they carry no code, so enable is the only moment they can take
    # effect. Idempotent, and never undone by disable_plugin.
    await materialise_plugin_data(db, tenant_id=tenant_id, version=version)
    return inst


async def disable_plugin(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    capa_id: uuid.UUID,
    reason: str | None = None,
) -> CapaInstallation:
    plugin = await db.get(Capa, capa_id)
    if plugin is None:
        raise PluginError("plugin not found")
    inst = await _installation(db, tenant_id, capa_id)
    was_enabled = inst.status == "enabled"
    inst.status = "disabled"
    inst.disabled_reason = reason
    await db.flush()
    if was_enabled:
        await _unregister_and_teardown(get_hook_registry(tenant_id), capa_id)
        _unregister_claude_hooks(tenant_id, capa_id)
    return inst


async def record_failure(
    db: AsyncSession, *, tenant_id: uuid.UUID, capa_id: uuid.UUID
) -> CapaInstallation:
    plugin = await db.get(Capa, capa_id)
    if plugin is None:
        raise PluginError("plugin not found")
    inst = await _installation(db, tenant_id, capa_id)
    inst.failure_count += 1
    if inst.failure_count >= CIRCUIT_BREAKER_THRESHOLD and inst.status != "quarantined":
        inst.status = "quarantined"
        inst.disabled_reason = "circuit breaker: too many hook failures"
        await _unregister_and_teardown(get_hook_registry(tenant_id), capa_id)
        _unregister_claude_hooks(tenant_id, capa_id)
    await db.flush()
    return inst
