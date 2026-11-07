import { createContext, useContext, type ComponentType, type ReactNode } from "react";
import type {
  EditionBanner,
  EditionNavigationItem,
  EditionOnboarding,
  EditionProfileSection,
  EditionSidebarSlotProps,
  FrontendEditionExtension,
} from "./contracts";

export interface FrontendEditionComposition {
  readonly navigation: readonly EditionNavigationItem[];
  readonly capabilities: ReadonlySet<string>;
  readonly banners: readonly EditionBanner[];
  /** Navigation targets whose extension declares capabilities. */
  readonly gates: readonly EditionCapabilityGateRule[];
  readonly onboarding: EditionOnboarding | null;
  readonly sidebarHeader: ComponentType<EditionSidebarSlotProps> | null;
  readonly sidebarFooter: ComponentType<EditionSidebarSlotProps> | null;
  readonly profileSections: readonly EditionProfileSection[];
}

export interface EditionCapabilityGateRule {
  /** The navigation item this rule belongs to. */
  readonly id: string;
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

const trimSlashes = (path: string) => (path.length > 1 ? path.replace(/\/+$/, "") || "/" : path);

/** The navigation items the caller may use: an item is dropped only when its own
 *  extension declares capabilities and the known capabilities lack one. */
export function visibleEditionNavigation(
  navigation: readonly EditionNavigationItem[],
  gates: readonly EditionCapabilityGateRule[],
  callerCapabilities: readonly string[] | null,
): readonly EditionNavigationItem[] {
  return navigation.filter(
    (item) => !gates.some((g) => g.id === item.id && lacksCapabilities(g, callerCapabilities)),
  );
}

/** Whether a path belongs to an edition feature the caller cannot use. */
export function isPathBlocked(
  gates: readonly EditionCapabilityGateRule[],
  path: string,
  callerCapabilities: readonly string[] | null,
): boolean {
  const p = trimSlashes(path);
  return gates.some((g) => {
    const to = trimSlashes(g.to);
    return (p === to || p.startsWith(`${to}/`)) && lacksCapabilities(g, callerCapabilities);
  });
}

const emptyComposition: FrontendEditionComposition = {
  navigation: [],
  capabilities: new Set(),
  banners: [],
  gates: [],
  onboarding: null,
  sidebarHeader: null,
  sidebarFooter: null,
  profileSections: [],
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
  let onboarding: EditionOnboarding | null = null;
  let sidebarHeader: ComponentType<EditionSidebarSlotProps> | null = null;
  let sidebarFooter: ComponentType<EditionSidebarSlotProps> | null = null;
  const profileSectionIds = new Set<string>();
  const profileSections: EditionProfileSection[] = [];

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
        gates.push({ id: item.id, to: item.to, capabilities: extension.capabilities });
    }
    for (const capability of extension.capabilities ?? []) capabilities.add(capability);
    for (const banner of extension.banners ?? []) {
      if (bannerIds.has(banner.id)) throw new Error(`Duplicate edition banner: ${banner.id}`);
      bannerIds.add(banner.id);
      banners.push(banner);
    }
    if (extension.onboarding) {
      if (onboarding) throw new Error(`Duplicate edition onboarding: ${extension.id}`);
      onboarding = extension.onboarding;
    }
    if (extension.sidebarHeader) {
      if (sidebarHeader) throw new Error(`Duplicate edition sidebar header: ${extension.id}`);
      sidebarHeader = extension.sidebarHeader;
    }
    if (extension.sidebarFooter) {
      if (sidebarFooter) throw new Error(`Duplicate edition sidebar footer: ${extension.id}`);
      sidebarFooter = extension.sidebarFooter;
    }
    for (const section of extension.profileSections ?? []) {
      if (profileSectionIds.has(section.id))
        throw new Error(`Duplicate edition profile section: ${section.id}`);
      profileSectionIds.add(section.id);
      profileSections.push(section);
    }
  }

  return {
    navigation,
    capabilities,
    banners,
    gates,
    onboarding,
    sidebarHeader,
    sidebarFooter,
    profileSections,
  };
}

/** Paths an edition's onboarding owns; they render without the app shell. */
export function editionBarePaths(
  composition: Pick<FrontendEditionComposition, "onboarding">,
): readonly string[] {
  const onboarding = composition.onboarding;
  if (!onboarding) return [];
  return onboarding.memberRoute
    ? [onboarding.adminRoute, onboarding.memberRoute]
    : [onboarding.adminRoute];
}

export interface OnboardingCaller {
  readonly onboardingStatus?: string | null;
  readonly memberOnboardingStatus?: string | null;
}

/** Where `/` sends the caller first, or null to stay. `managesDepartments` is
 *  null while the caller's rights are still loading: deciding then could send
 *  an admin to the member welcome. */
export function onboardingRedirect(
  me: OnboardingCaller | undefined,
  managesDepartments: boolean | null,
  onboarding: EditionOnboarding | null,
): string | null {
  if (!me || managesDepartments === null) return null;
  if (me.onboardingStatus === "pending" && managesDepartments)
    return onboarding?.adminRoute ?? "/welcome";
  if (onboarding?.memberRoute && me.memberOnboardingStatus === "pending")
    return onboarding.memberRoute;
  return null;
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
