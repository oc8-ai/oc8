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
