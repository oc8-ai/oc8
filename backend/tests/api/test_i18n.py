from __future__ import annotations

from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from oc8.config import get_settings
from oc8.i18n.catalog import invalidate_core_catalog_cache
from oc8.main import create_app

pytestmark = pytest.mark.asyncio


async def _get_core_catalog(monkeypatch: pytest.MonkeyPatch, root: Path) -> dict[str, object]:
    settings = get_settings().model_copy(update={"core_i18n_path": str(root)})
    monkeypatch.setattr("oc8.i18n.catalog.get_settings", lambda: settings)
    invalidate_core_catalog_cache()
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/api/v1/i18n/core")
            assert r.status_code == 200, r.text
            body = r.json()
            assert isinstance(body, dict)
            return body


async def test_core_i18n_endpoint_is_unauthenticated(tmp_path: Path) -> None:
    """No `Authorization` header at all -- reachable before login, like
    `/auth/config`, since the login screen itself is translated."""
    invalidate_core_catalog_cache()
    app = create_app()
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            r = await client.get("/api/v1/i18n/core")
            assert r.status_code == 200, r.text


async def test_core_i18n_endpoint_empty_when_no_catalogs_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    body = await _get_core_catalog(monkeypatch, tmp_path)
    assert body["locales"] == []


async def test_core_i18n_endpoint_serves_a_locale(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "fr.po").write_text(
        'msgid ""\n'
        'msgstr ""\n'
        '"X-Native-Name: Français\\n"\n'
        '"X-Flag: 🇫🇷\\n"\n'
        "\n"
        'msgid "Save"\n'
        'msgstr "Enregistrer"\n',
        encoding="utf-8",
    )
    body = await _get_core_catalog(monkeypatch, tmp_path)
    assert body["locales"] == [
        {
            "locale": "fr",
            "nativeName": "Français",
            "flag": "🇫🇷",
            "translations": {"Save": "Enregistrer"},
        }
    ]


_FR_HEADER = 'msgid ""\nmsgstr ""\n"X-Native-Name: Français\\n"\n"X-Flag: 🇫🇷\\n"\n\n'
_MIXED = (
    _FR_HEADER + 'msgid "Save"\nmsgstr "Enregistrer"\n\n'
    '#, fuzzy\nmsgid "Cancel"\nmsgstr "Annuler"\n\n'
    '#, fuzzy\nmsgid "Delete"\nmsgstr ""\n\n'
    'msgid "Close"\nmsgstr ""\n'
)


def _catalogs_with(
    monkeypatch: pytest.MonkeyPatch, root: Path, drafts: bool, edition: tuple[Path, ...] = ()
) -> dict[str, object]:
    from oc8.i18n.catalog import load_core_catalogs

    settings = get_settings().model_copy(
        update={"core_i18n_path": str(root), "i18n_serve_drafts": drafts}
    )
    monkeypatch.setattr("oc8.i18n.catalog.get_settings", lambda: settings)
    invalidate_core_catalog_cache()
    return dict(load_core_catalogs(edition_dirs=edition))


def test_fuzzy_entries_are_dropped_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "fr.po").write_text(_MIXED, encoding="utf-8")
    cats = _catalogs_with(monkeypatch, tmp_path, drafts=False)
    assert cats["fr"].translations == {"Save": "Enregistrer"}  # type: ignore[attr-defined]


def test_fuzzy_entries_are_served_with_the_setting_but_never_empty_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "fr.po").write_text(_MIXED, encoding="utf-8")
    cats = _catalogs_with(monkeypatch, tmp_path, drafts=True)
    assert cats["fr"].translations == {  # type: ignore[attr-defined]
        "Save": "Enregistrer",
        "Cancel": "Annuler",
    }


def test_edition_fuzzy_entries_follow_the_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    core = tmp_path / "core"
    edition = tmp_path / "edition"
    core.mkdir()
    edition.mkdir()
    (core / "fr.po").write_text(_FR_HEADER + 'msgid "Save"\nmsgstr "Enregistrer"\n', "utf-8")
    (edition / "fr.po").write_text('#, fuzzy\nmsgid "Audit"\nmsgstr "Audit FR"\n', "utf-8")
    off = _catalogs_with(monkeypatch, core, drafts=False, edition=(edition,))
    assert off["fr"].translations == {"Save": "Enregistrer"}  # type: ignore[attr-defined]
    on = _catalogs_with(monkeypatch, core, drafts=True, edition=(edition,))
    assert on["fr"].translations == {  # type: ignore[attr-defined]
        "Save": "Enregistrer",
        "Audit": "Audit FR",
    }


def test_a_locale_with_no_served_strings_is_not_listed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    only_drafts = _FR_HEADER + '#, fuzzy\nmsgid "Save"\nmsgstr "Enregistrer"\n'
    (tmp_path / "fr.po").write_text(only_drafts, encoding="utf-8")
    assert _catalogs_with(monkeypatch, tmp_path, drafts=False) == {}
    assert "fr" in _catalogs_with(monkeypatch, tmp_path, drafts=True)
