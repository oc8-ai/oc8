from __future__ import annotations

from pathlib import Path

from oc8.i18n.catalog import invalidate_core_catalog_cache, load_core_catalogs


def _write_po(dir_: Path, locale: str, body: str) -> None:
    (dir_ / f"{locale}.po").write_text(body, encoding="utf-8")


def test_load_core_catalogs_missing_directory_returns_empty(tmp_path: Path) -> None:
    invalidate_core_catalog_cache()
    assert load_core_catalogs(str(tmp_path / "does-not-exist")) == {}


def test_load_core_catalogs_reads_translations_and_metadata(tmp_path: Path) -> None:
    invalidate_core_catalog_cache()
    _write_po(
        tmp_path,
        "fr",
        'msgid ""\n'
        'msgstr ""\n'
        '"X-Native-Name: Français\\n"\n'
        '"X-Flag: 🇫🇷\\n"\n'
        "\n"
        'msgid "Save"\n'
        'msgstr "Enregistrer"\n',
    )
    catalogs = load_core_catalogs(str(tmp_path))
    assert set(catalogs) == {"fr"}
    fr = catalogs["fr"]
    assert fr.native_name == "Français"
    assert fr.flag == "🇫🇷"
    assert fr.translations == {"Save": "Enregistrer"}


def test_load_core_catalogs_missing_headers_falls_back_to_locale_code(tmp_path: Path) -> None:
    invalidate_core_catalog_cache()
    _write_po(tmp_path, "es", 'msgid "Save"\nmsgstr "Guardar"\n')
    catalogs = load_core_catalogs(str(tmp_path))
    assert catalogs["es"].native_name == "es"
    assert catalogs["es"].flag == ""


def test_load_core_catalogs_omits_untranslated_entries(tmp_path: Path) -> None:
    invalidate_core_catalog_cache()
    _write_po(tmp_path, "fr", 'msgid "Save"\nmsgstr ""\n')
    assert load_core_catalogs(str(tmp_path))["fr"].translations == {}


def test_load_core_catalogs_skips_malformed_file_without_failing_others(
    tmp_path: Path,
) -> None:
    invalidate_core_catalog_cache()
    _write_po(tmp_path, "fr", 'msgid "Save"\nmsgstr "Enregistrer"\n')
    (tmp_path / "broken.po").write_text("not a po file at all {{{", encoding="utf-8")
    catalogs = load_core_catalogs(str(tmp_path))
    assert set(catalogs) == {"fr"}


def test_load_core_catalogs_is_cached_until_invalidated(tmp_path: Path) -> None:
    invalidate_core_catalog_cache()
    _write_po(tmp_path, "fr", 'msgid "Save"\nmsgstr "Enregistrer"\n')
    first = load_core_catalogs(str(tmp_path))
    _write_po(tmp_path, "fr", 'msgid "Save"\nmsgstr "Changed"\n')
    assert load_core_catalogs(str(tmp_path)) is first
    invalidate_core_catalog_cache()
    assert load_core_catalogs(str(tmp_path))["fr"].translations == {"Save": "Changed"}


def test_load_core_catalogs_omits_fuzzy_entries(tmp_path: Path) -> None:
    invalidate_core_catalog_cache()
    _write_po(
        tmp_path,
        "nl",
        'msgid "Save"\nmsgstr "Opslaan"\n'
        "\n"
        "#, fuzzy\n"
        'msgid "Cancel"\nmsgstr "Annuleer (concept)"\n'
        "\n"
        'msgid "Delete"\nmsgstr ""\n',
    )
    assert load_core_catalogs(str(tmp_path))["nl"].translations == {"Save": "Opslaan"}
