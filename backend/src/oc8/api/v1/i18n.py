"""Serves the core UI translation catalogs (`oc8.i18n.catalog`) the frontend
fetches once at startup to resolve `t("Save")`-style lookups.

Public and unauthenticated like `/auth/config`: a browser needs this before
it has any session at all (the login screen itself is translated), and the
payload carries nothing more sensitive than button labels. An edition's own
catalogs (`app.state.i18n_catalog_dirs`, validated by `create_app`) are
merged in under core's.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from oc8.api.deps import unguarded
from oc8.i18n.catalog import load_core_catalogs
from oc8.schemas.dto import CoreI18nCatalogDTO, CoreLocaleDTO

router = APIRouter()


@router.get(
    "/i18n/core",
    response_model=CoreI18nCatalogDTO,
    dependencies=[Depends(unguarded("read before login: the login screen itself is translated"))],
)
async def core_i18n_catalog(request: Request) -> CoreI18nCatalogDTO:
    catalogs = load_core_catalogs(edition_dirs=getattr(request.app.state, "i18n_catalog_dirs", ()))
    return CoreI18nCatalogDTO(
        locales=[
            CoreLocaleDTO(
                locale=catalog.locale,
                native_name=catalog.native_name,
                flag=catalog.flag,
                translations=catalog.translations,
            )
            for catalog in sorted(catalogs.values(), key=lambda c: c.locale)
        ]
    )
