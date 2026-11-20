"""EditionExtension.i18n_catalog_dirs(): validated at boot, served by /i18n/core."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient

from oc8.config import get_settings
from oc8.edition import EditionExtension
from oc8.i18n.catalog import collect_edition_catalog_dirs, invalidate_core_catalog_cache
from oc8.main import create_app


class _CatalogEdition(EditionExtension):
    def __init__(self, *dirs: object) -> None:
        self._dirs = dirs

    def routers(self) -> Sequence[APIRouter]:
        return ()

    def i18n_catalog_dirs(self) -> Sequence[Path]:
        return self._dirs  # type: ignore[return-value]


class _StructuralEdition:
    """Satisfies the Protocol structurally, without i18n_catalog_dirs()."""

    def routers(self) -> Sequence[APIRouter]:
        return ()

    def tools(self) -> tuple[()]:
        return ()


def test_extension_without_the_method_contributes_no_catalogs() -> None:
    assert collect_edition_catalog_dirs([_StructuralEdition()]) == ()


def test_protocol_default_contributes_no_catalogs() -> None:
    assert collect_edition_catalog_dirs([_CatalogEdition()]) == ()


def test_a_valid_directory_is_collected_once(tmp_path: Path) -> None:
    (tmp_path / "fr.po").write_text('msgid "Save"\nmsgstr "Enregistrer"\n', encoding="utf-8")
    assert collect_edition_catalog_dirs([_CatalogEdition(tmp_path), _CatalogEdition(tmp_path)]) == (
        tmp_path,
    )


def test_a_missing_directory_stops_startup_naming_the_extension(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"edition _CatalogEdition: .*does not exist"):
        create_app(edition_extensions=[_CatalogEdition(tmp_path / "nope")])


def test_a_file_that_does_not_parse_stops_startup(tmp_path: Path) -> None:
    (tmp_path / "fr.po").write_text("not a po file at all {{{", encoding="utf-8")
    with pytest.raises(ValueError, match=r"edition _CatalogEdition: .*fr\.po does not parse"):
        create_app(edition_extensions=[_CatalogEdition(tmp_path)])


def test_a_non_path_entry_stops_startup(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match=r"edition _CatalogEdition: .*returned str"):
        create_app(edition_extensions=[_CatalogEdition(str(tmp_path))])


@pytest.mark.asyncio
async def test_core_endpoint_serves_the_edition_strings_under_cores(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    core = tmp_path / "core"
    edition = tmp_path / "edition"
    core.mkdir()
    edition.mkdir()
    (core / "fr.po").write_text(
        'msgid ""\nmsgstr ""\n"X-Native-Name: Français\\n"\n"X-Flag: 🇫🇷\\n"\n\n'
        'msgid "Save"\nmsgstr "Enregistrer"\n',
        encoding="utf-8",
    )
    (edition / "fr.po").write_text(
        'msgid "Save"\nmsgstr "Sauver"\n\nmsgid "Supervision"\nmsgstr "Supervision"\n',
        encoding="utf-8",
    )
    settings = get_settings().model_copy(update={"core_i18n_path": str(core)})
    monkeypatch.setattr("oc8.i18n.catalog.get_settings", lambda: settings)
    invalidate_core_catalog_cache()
    app = create_app(edition_extensions=[_CatalogEdition(edition)])

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get("/api/v1/i18n/core")

    assert response.status_code == 200, response.text
    assert response.json()["locales"] == [
        {
            "locale": "fr",
            "nativeName": "Français",
            "flag": "🇫🇷",
            "translations": {"Save": "Enregistrer", "Supervision": "Supervision"},
        }
    ]
