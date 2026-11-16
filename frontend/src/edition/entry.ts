import type { EditionServerHooks, FrontendEditionExtension } from "./contracts";
import type { AnyRoute } from "@tanstack/react-router";

/**
 * The Community build's explicit edition entry point. Enterprise replaces this
 * module at build time; Community must never discover or import Enterprise.
 */
export const editionExtensions: readonly FrontendEditionExtension[] = [];
export const editionRoutes: readonly AnyRoute[] = [];
export const editionServer: EditionServerHooks = {};
