"""Loads `<i18n_path>/<locale>.po` into the payload `api/v1/i18n.py` serves.

Mirrors `capas.discovery._read_i18n_folder`: `msgid` is the English source
string itself (content-as-key, no invented keys), and `polib` does the
parsing. The two differ in where a bad file leads -- a malformed *capa*
catalog invalidates that one capa (`ManifestError`), but a malformed *core*
locale here is skipped with a warning so one broken translation file a
partner pushed can't take the whole UI down for every language.

An edition (`oc8.edition.EditionExtension.i18n_catalog_dirs`) may ship its
own `<locale>.po` files for the strings its UI adds. They are merged into
the same per-locale catalog, never into a parallel one, and core always
wins: a msgid core's catalog for that locale carries -- translated or not --
belongs to core, so an edition can add text but never change core's.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from oc8.config import get_settings

if TYPE_CHECKING:
    import polib

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 5.0
_cache: dict[tuple[str, tuple[str, ...], bool], tuple[float, dict[str, CoreCatalog]]] = {}


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
    #: ones (Weblate's "Needs editing", e.g. unreviewed AI drafts; served when
    #: `i18n_serve_drafts` is on) are omitted, like `msgfmt` does -- a lookup
    #: miss falls back to the English source.
    translations: dict[str, str]


def load_core_catalogs(
    root: str | None = None, edition_dirs: Sequence[Path] = ()
) -> dict[str, CoreCatalog]:
    """Every `<root>/<locale>.po`, merged with each edition directory's
    `<locale>.po`, cached for `_CACHE_TTL_SECONDS`.

    `root` defaults to `get_settings().core_i18n_path` ("i18n", resolved
    against the working directory -- the same convention `capas_path` uses).
    A missing directory is not an error: it just means no language besides
    English has been added yet.

    `edition_dirs` are the directories `collect_edition_catalog_dirs`
    validated at boot. An edition entry is served only for a msgid core's
    catalog for that locale does not carry; an earlier directory wins over a
    later one. Untranslated and fuzzy edition entries are skipped like core's.
    `X-Native-Name`/`X-Flag` come from core's file when it sets them, else
    from the first edition file that does.
    """
    resolved_root = root if root is not None else get_settings().core_i18n_path
    drafts = get_settings().i18n_serve_drafts
    cache_key = (resolved_root, tuple(str(d) for d in edition_dirs), drafts)
    cached = _cache.get(cache_key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    #: locale -> (native name or None, flag or None, msgids owned, translations)
    merged: dict[str, tuple[str | None, str | None, set[str], dict[str, str]]] = {}
    for path, po in _read_dir(Path(resolved_root), "core"):
        merged[path.stem] = (
            po.metadata.get("X-Native-Name"),
            po.metadata.get("X-Flag"),
            {entry.msgid for entry in po},
            _served(po, drafts),
        )
    for edition_dir in edition_dirs:
        for path, po in _read_dir(edition_dir, "edition"):
            native, flag, owned, translations = merged.get(path.stem, (None, None, set(), {}))
            for msgid, msgstr in _served(po, drafts).items():
                if msgid not in owned:
                    translations.setdefault(msgid, msgstr)
            merged[path.stem] = (
                native if native is not None else po.metadata.get("X-Native-Name"),
                flag if flag is not None else po.metadata.get("X-Flag"),
                owned,
                translations,
            )

    out = {
        locale: CoreCatalog(
            locale=locale,
            native_name=native or locale,
            flag=flag or "",
            translations=translations,
        )
        for locale, (native, flag, _owned, translations) in merged.items()
        # A locale with nothing to serve would only show English under its own
        # name in the language picker, so it is not offered.
        if translations
    }
    _cache[cache_key] = (now, out)
    return out


def _read_dir(base: Path, kind: str) -> Iterable[tuple[Path, polib.POFile]]:
    """Each parseable `<base>/<locale>.po`, sorted; a malformed one is skipped
    with a warning (see the module docstring)."""
    import polib

    if not base.is_dir():
        return
    for path in sorted(base.glob("*.po")):
        try:
            po = polib.pofile(str(path))
        except Exception as exc:  # polib raises bare IOError/ValueError variants
            logger.warning("skipping invalid %s i18n catalog %s: %s", kind, path, exc)
            continue
        yield path, po


def _served(po: polib.POFile, drafts: bool = False) -> dict[str, str]:
    """Translated entries; fuzzy ones only when `i18n_serve_drafts` is on.
    An empty msgstr is never served."""
    return {
        entry.msgid: entry.msgstr for entry in po if entry.msgstr and (drafts or not entry.fuzzy)
    }


def collect_edition_catalog_dirs(extensions: Iterable[object]) -> tuple[Path, ...]:
    """The catalog directories editions contribute, refused at boot when
    malformed (as `collect_edition_templates` refuses a bad agent template).

    An extension without `i18n_catalog_dirs()` contributes none. Each entry
    must be a `pathlib.Path` naming an existing directory whose every
    `<locale>.po` parses -- a broken edition catalog is a packaging bug the
    edition ships, so it stops startup instead of being skipped at runtime.
    """
    import polib

    out: list[Path] = []
    for ext in extensions:
        hook = getattr(ext, "i18n_catalog_dirs", None)
        if hook is None:
            continue
        who = type(ext).__name__
        for item in hook():
            if not isinstance(item, Path):
                raise TypeError(
                    f"edition {who}: i18n_catalog_dirs() returned {type(item).__name__}, "
                    "not a pathlib.Path"
                )
            if not item.is_dir():
                raise ValueError(f"edition {who}: i18n catalog directory {item} does not exist")
            for path in sorted(item.glob("*.po")):
                try:
                    polib.pofile(str(path))
                except Exception as exc:
                    raise ValueError(
                        f"edition {who}: i18n catalog {path} does not parse: {exc}"
                    ) from exc
            if item not in out:
                out.append(item)
    return tuple(out)


def invalidate_core_catalog_cache() -> None:
    """For tests: forget cached catalogs so the next load re-reads disk."""
    _cache.clear()
