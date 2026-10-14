import { createContext, useContext, type ReactNode } from "react";
import type { EditionBanner, EditionNavigationItem, FrontendEditionExtension } from "./contracts";

export interface FrontendEditionComposition {
  readonly navigation: readonly EditionNavigationItem[];
  readonly capabilities: ReadonlySet<string>;
  readonly banners: readonly EditionBanner[];
  /** Navigation targets whose extension declares capabilities. */
  readonly gates: readonly EditionCapabilityGateRule[];
}

export interface EditionCapabilityGateRule {
  readonly to: string;
  readonly capabilities: readonly string[];
}

/** True when the caller's capabilities are known and lack one the rule needs.
 *  A null list (unknown) never blocks. */
export function lacksCapabilities(
  rule: EditionCapabilityGateRule,
  callerCapabilities: readonly string[] | null,
): boolean {
  return (
    callerCapabilities !== null && rule.capabilities.some((c) => !callerCapabilities.includes(c))
  );
}

/** Whether a path belongs to an edition feature the caller cannot use. */
export function isPathBlocked(
  gates: readonly EditionCapabilityGateRule[],
  path: string,
  callerCapabilities: readonly string[] | null,
): boolean {
  return gates.some(
    (g) =>
      (path === g.to || path.startsWith(`${g.to}/`)) && lacksCapabilities(g, callerCapabilities),
  );
}

const emptyComposition: FrontendEditionComposition = {
  navigation: [],
  capabilities: new Set(),
  banners: [],
  gates: [],
};

const EditionCompositionContext = createContext<FrontendEditionComposition>(emptyComposition);

/** Compose trusted, build-time edition contributions and reject duplicate ids. */
export function composeFrontendEdition(
  extensions: readonly FrontendEditionExtension[],
): FrontendEditionComposition {
  const extensionIds = new Set<string>();
  const navigationIds = new Set<string>();
  const navigation: EditionNavigationItem[] = [];
  const capabilities = new Set<string>();
  const bannerIds = new Set<string>();
  const banners: EditionBanner[] = [];
  const gates: EditionCapabilityGateRule[] = [];

  for (const extension of extensions) {
    if (extensionIds.has(extension.id))
      throw new Error(`Duplicate edition extension: ${extension.id}`);
    extensionIds.add(extension.id);
    for (const item of extension.navigation ?? []) {
      if (navigationIds.has(item.id))
        throw new Error(`Duplicate edition navigation item: ${item.id}`);
      navigationIds.add(item.id);
      navigation.push(item);
      if (extension.capabilities?.length)
        gates.push({ to: item.to, capabilities: extension.capabilities });
    }
    for (const capability of extension.capabilities ?? []) capabilities.add(capability);
    for (const banner of extension.banners ?? []) {
      if (bannerIds.has(banner.id)) throw new Error(`Duplicate edition banner: ${banner.id}`);
      bannerIds.add(banner.id);
      banners.push(banner);
    }
  }

  return { navigation, capabilities, banners, gates };
}

export function EditionCompositionProvider({
  extensions,
  children,
}: {
  extensions: readonly FrontendEditionExtension[];
  children: ReactNode;
}) {
  return (
    <EditionCompositionContext.Provider value={composeFrontendEdition(extensions)}>
      {children}
    </EditionCompositionContext.Provider>
  );
}

export function useFrontendEdition(): FrontendEditionComposition {
  return useContext(EditionCompositionContext);
}
