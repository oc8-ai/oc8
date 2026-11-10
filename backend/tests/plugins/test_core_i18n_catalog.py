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


# --- Edition catalogs (EditionExtension.i18n_catalog_dirs) -------------------


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    core = tmp_path / "core"
    edition = tmp_path / "edition"
    core.mkdir()
    edition.mkdir()
    return core, edition


def test_edition_catalog_adds_its_own_strings(tmp_path: Path) -> None:
    invalidate_core_catalog_cache()
    core, edition = _dirs(tmp_path)
    _write_po(core, "fr", 'msgid "Save"\nmsgstr "Enregistrer"\n')
    _write_po(edition, "fr", 'msgid "SAML SSO"\nmsgstr "SSO SAML"\n')
    catalogs = load_core_catalogs(str(core), edition_dirs=(edition,))
    assert catalogs["fr"].translations == {"Save": "Enregistrer", "SAML SSO": "SSO SAML"}


def test_core_wins_over_an_edition_on_the_same_msgid(tmp_path: Path) -> None:
    """Core's text is never replaced -- not even where core's own entry is
    still untranslated or an unreviewed draft: the msgid belongs to core."""
    invalidate_core_catalog_cache()
    core, edition = _dirs(tmp_path)
    _write_po(
        core,
        "fr",
        'msgid "Save"\nmsgstr "Enregistrer"\n'
        "\n"
        'msgid "Cancel"\nmsgstr ""\n'
        "\n"
        "#, fuzzy\n"
        'msgid "Delete"\nmsgstr "Supprimer"\n',
    )
    _write_po(
        edition,
        "fr",
        'msgid "Save"\nmsgstr "Sauver"\n'
        "\n"
        'msgid "Cancel"\nmsgstr "Annuler"\n'
        "\n"
        'msgid "Delete"\nmsgstr "Effacer"\n',
    )
    catalogs = load_core_catalogs(str(core), edition_dirs=(edition,))
    assert catalogs["fr"].translations == {"Save": "Enregistrer"}


def test_edition_fuzzy_and_empty_entries_are_skipped(tmp_path: Path) -> None:
    invalidate_core_catalog_cache()
    core, edition = _dirs(tmp_path)
    _write_po(
        edition,
        "nl",
        'msgid "Supervision"\nmsgstr "Toezicht"\n'
        "\n"
        "#, fuzzy\n"
        'msgid "SAML SSO"\nmsgstr "SAML SSO (concept)"\n'
        "\n"
        'msgid "Enabled"\nmsgstr ""\n',
    )
    catalogs = load_core_catalogs(str(core), edition_dirs=(edition,))
    assert catalogs["nl"].translations == {"Supervision": "Toezicht"}


def test_native_name_and_flag_come_from_core_when_it_sets_them(tmp_path: Path) -> None:
    invalidate_core_catalog_cache()
    core, edition = _dirs(tmp_path)
    _write_po(
        core,
        "fr",
        'msgid ""\nmsgstr ""\n"X-Native-Name: Français\\n"\n"X-Flag: 🇫🇷\\n"\n',
    )
    _write_po(
        edition,
        "fr",
        'msgid ""\nmsgstr ""\n"X-Native-Name: French\\n"\n"X-Flag: FR\\n"\n',
    )
    _write_po(
        edition,
        "es",
        'msgid ""\nmsgstr ""\n"X-Native-Name: Español\\n"\n"X-Flag: 🇪🇸\\n"\n'
        "\n"
        'msgid "Supervision"\nmsgstr "Supervisión"\n',
    )
    catalogs = load_core_catalogs(str(core), edition_dirs=(edition,))
    assert (catalogs["fr"].native_name, catalogs["fr"].flag) == ("Français", "🇫🇷")
    # A locale only the edition ships still shows up, named by its own header.
    assert (catalogs["es"].native_name, catalogs["es"].flag) == ("Español", "🇪🇸")
    assert catalogs["es"].translations == {"Supervision": "Supervisión"}


def test_no_edition_dirs_serves_core_alone(tmp_path: Path) -> None:
    invalidate_core_catalog_cache()
    core, _edition = _dirs(tmp_path)
    _write_po(core, "fr", 'msgid "Save"\nmsgstr "Enregistrer"\n')
    assert load_core_catalogs(str(core)) == load_core_catalogs(str(core), edition_dirs=())
