"""Container image a runtime capa declares, and the registry login that pulls it.

The image name lives on the runtime capa (`sandbox.image`). A setup field
named by `sandbox.registry_credential` may point at a `container_registry`
credential. That login is used only when the tag is missing locally. It is
never placed in the container environment.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8.capas.discovery import discover_plugins, find_plugin
from oc8.models import Capa, CapaInstallation
from oc8.sandbox.types import RegistryAuth
from oc8.secrets.service import SecretNotFound, resolve_secret


class RegistryLoginError(RuntimeError):
    """The runtime's registry login is present but cannot be used."""


def declared_runtime_images(paths: Sequence[str] | None = None) -> set[str]:
    """Every `sandbox.image` a runtime_adapter capa on disk declares."""
    images: set[str] = set()
    for plugin in discover_plugins(paths):
        if plugin.type != "runtime_adapter" or not plugin.valid or not plugin.manifest:
            continue
        image = (plugin.manifest.get("sandbox") or {}).get("image")
        if isinstance(image, str) and image.strip():
            images.add(image.strip())
    return images


def runtime_image(
    plugin_name: str, fallback: str, paths: Sequence[str] | None = None
) -> str:
    """The capa's `sandbox.image` when it set one, otherwise `fallback`."""
    found = find_plugin(plugin_name, paths)
    if found is None or not found.manifest:
        return fallback
    image = (found.manifest.get("sandbox") or {}).get("image")
    if isinstance(image, str) and image.strip():
        return image.strip()
    return fallback


def _registry_credential_key(
    plugin_name: str, paths: Sequence[str] | None
) -> str:
    found = find_plugin(plugin_name, paths)
    if found is None or not found.manifest:
        return ""
    raw = (found.manifest.get("sandbox") or {}).get("registry_credential") or ""
    return str(raw).strip()


async def registry_auth_for_runtime(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    plugin_name: str,
    paths: Sequence[str] | None = None,
) -> RegistryAuth | None:
    """The registry login saved on this runtime's setup, or None when unset.

    A declared credential field that was left empty stays None, so a public
    image still pulls anonymously. A username without its stored token is an
    error: falling through to an anonymous pull would hide a half-saved login.
    """
    key = _registry_credential_key(plugin_name, paths)
    if not key:
        return None
    plugin = (
        await db.execute(select(Capa).where(Capa.tenant_id == tenant_id, Capa.name == plugin_name))
    ).scalar_one_or_none()
    if plugin is None:
        return None
    installation = (
        await db.execute(
            select(CapaInstallation).where(
                CapaInstallation.tenant_id == tenant_id,
                CapaInstallation.capa_id == plugin.id,
            )
        )
    ).scalar_one_or_none()
    config = dict(installation.config) if installation is not None else {}
    username = str(config.get("username") or "").strip()
    registry = str(config.get("registry") or "").strip()
    if not username:
        return None
    try:
        password = await resolve_secret(
            db, tenant_id=tenant_id, ref=f"plugin:{plugin_name}:{key}"
        )
    except SecretNotFound as exc:
        raise RegistryLoginError(
            f"runtime {plugin_name!r} has a registry username but no stored token; "
            "save the registry login in that runtime's setup"
        ) from exc
    return RegistryAuth(username=username, password=password, registry=registry)
