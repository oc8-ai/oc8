import type { LucideIcon } from "lucide-react";
import type { ComponentType } from "react";

/**
 * Build-time contribution from a trusted product edition. This is deliberately
 * separate from the Marketplace plugin contract: an edition is shipped with
 * the application, while plugins remain tenant-installed artefacts.
 */
export interface FrontendEditionExtension {
  readonly id: string;
  readonly navigation?: readonly EditionNavigationItem[];
  readonly capabilities?: readonly string[];
  readonly banners?: readonly EditionBanner[];
  /** Where a pending organisation admin, and a member whose own welcome is
   *  pending, are sent instead of Community's `/welcome`. At most one
   *  extension may supply it. */
  readonly onboarding?: EditionOnboarding;
  /** Rendered at the top of the sidebar, above the navigation. At most one. */
  readonly sidebarHeader?: ComponentType<EditionSidebarSlotProps>;
  /** Rendered at the bottom of the sidebar, below the navigation. At most one. */
  readonly sidebarFooter?: ComponentType<EditionSidebarSlotProps>;
  /** Extra panels appended to the profile page, in extension order. */
  readonly profileSections?: readonly EditionProfileSection[];
}

/** An edition's own first-run screens. Both render without the app shell. */
export interface EditionOnboarding {
  /** For a caller who may manage departments while the organisation is pending. */
  readonly adminRoute: string;
  /** For any caller whose own `memberOnboardingStatus` is pending. */
  readonly memberRoute?: string;
}

export interface EditionSidebarSlotProps {
  /** The sidebar is collapsed to its icon rail. */
  readonly collapsed: boolean;
}

export interface EditionProfileSection {
  readonly id: string;
  readonly component: ComponentType;
}

/** A notice an edition shows above every page. */
export interface EditionBanner {
  readonly id: string;
  /** Rendered above every page; return null to show nothing. */
  readonly component: ComponentType;
}

/** A sidebar entry that an edition adds to the shared Community shell. */
export interface EditionNavigationItem {
  readonly id: string;
  readonly to: string;
  readonly label: LocalizedLabel;
  readonly icon: LucideIcon;
  readonly needs?: string;
  readonly strict?: boolean;
}

export interface LocalizedLabel {
  readonly en: string;
  readonly de: string;
}
