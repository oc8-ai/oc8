import { createContext, useContext, type ReactNode } from "react";
import type {
  EditionBanner,
  EditionNavigationItem,
  FrontendEditionExtension,
} from "./contracts";

export interface FrontendEditionComposition {
  readonly navigation: readonly EditionNavigationItem[];
  readonly capabilities: ReadonlySet<string>;
  readonly banners: readonly EditionBanner[];
}

const emptyComposition: FrontendEditionComposition = {
  navigation: [],
  capabilities: new Set(),
  banners: [],
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

  for (const extension of extensions) {
    if (extensionIds.has(extension.id)) throw new Error(`Duplicate edition extension: ${extension.id}`);
    extensionIds.add(extension.id);
    for (const item of extension.navigation ?? []) {
      if (navigationIds.has(item.id)) throw new Error(`Duplicate edition navigation item: ${item.id}`);
      navigationIds.add(item.id);
      navigation.push(item);
    }
    for (const capability of extension.capabilities ?? []) capabilities.add(capability);
    for (const banner of extension.banners ?? []) {
      if (bannerIds.has(banner.id)) throw new Error(`Duplicate edition banner: ${banner.id}`);
      bannerIds.add(banner.id);
      banners.push(banner);
    }
  }

  return { navigation, capabilities, banners };
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
