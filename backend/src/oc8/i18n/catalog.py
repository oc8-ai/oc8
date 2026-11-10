"""Loads `<i18n_path>/<locale>.po` into the payload `api/v1/i18n.py` serves.

Mirrors `capas.discovery._read_i18n_folder`: `msgid` is the English source
string itself (content-as-key, no invented keys), and `polib` does the
parsing. The two differ in where a bad file leads -- a malformed *capa*
catalog invalidates that one capa (`ManifestError`), but a malformed *core*
locale here is skipped with a warning so one broken translation file a
partner pushed can't take the whole UI down for every language.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from oc8.config import get_settings

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 5.0
_cache: dict[str, tuple[float, dict[str, CoreCatalog]]] = {}


@dataclass(frozen=True)
class CoreCatalog:
    locale: str
    #: `X-Native-Name` header, e.g. "Français". Falls back to the locale
    #: code itself when a `.po` file omits it, so a new language still shows
    #: up in the switcher even before someone fills in the display metadata.
    native_name: str
    #: `X-Flag` header, e.g. "🇫🇷". Empty string when absent -- the frontend
    #: renders no flag rather than a placeholder.
    flag: str
    #: `{msgid: msgstr}`. Untranslated entries (empty `msgstr`) and `fuzzy`
    #: ones (Weblate's "Needs editing", e.g. unreviewed AI drafts) are
    #: omitted, like `msgfmt` does -- a lookup miss falls back to the English
    #: source.
    translations: dict[str, str]


def load_core_catalogs(root: str | None = None) -> dict[str, CoreCatalog]:
    """Every `<root>/<locale>.po`, cached for `_CACHE_TTL_SECONDS`.

    `root` defaults to `get_settings().core_i18n_path` ("i18n", resolved
    against the working directory -- the same convention `capas_path` uses).
    A missing directory is not an error: it just means no language besides
    English has been added yet.
    """
    resolved_root = root if root is not None else get_settings().core_i18n_path
    cached = _cache.get(resolved_root)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    import polib

    out: dict[str, CoreCatalog] = {}
    base = Path(resolved_root)
    if base.is_dir():
        for path in sorted(base.glob("*.po")):
            locale = path.stem
            try:
                po = polib.pofile(str(path))
            except Exception as exc:  # polib raises bare IOError/ValueError variants
                logger.warning("skipping invalid core i18n catalog %s: %s", path, exc)
                continue
            out[locale] = CoreCatalog(
                locale=locale,
                native_name=po.metadata.get("X-Native-Name", locale),
                flag=po.metadata.get("X-Flag", ""),
                translations={
                    entry.msgid: entry.msgstr for entry in po if entry.msgstr and not entry.fuzzy
                },
            )

    _cache[resolved_root] = (now, out)
    return out


def invalidate_core_catalog_cache() -> None:
    """For tests: forget cached catalogs so the next load re-reads disk."""
    _cache.clear()
