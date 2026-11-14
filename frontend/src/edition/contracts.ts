import type { LucideIcon } from "lucide-react";
import type { ComponentType, ReactNode } from "react";

import type { ChatMessageDTO } from "@/lib/hooks-chat";
import type { CustomServerDraft, HttpToolsSnapshot } from "@/lib/http-tools";

export type {
  CustomServerDraft,
  HttpLoginDraft,
  HttpMethod,
  HttpToolDef,
  HttpToolsSnapshot,
  ImportMetadata,
  ParamLocation,
  ToolScopes,
} from "@/lib/http-tools";

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
  /** Cards an edition shows at the top of the home page, in extension order. */
  readonly homeSections?: readonly EditionHomeSection[];
  /** Chat slots (spec §6.2). Each entry may name the capabilities it needs. */
  readonly chat?: EditionChatContributions;
  /** Extra ways to describe a plain HTTP API in the custom server wizard,
   *  shown above "describe them by hand", in extension order. */
  readonly customServerSources?: readonly CustomServerSource[];
}

export type Capability = string;

interface ChatSlotEntry {
  readonly id: string;
  /** Capabilities the caller must all hold for the entry to render. */
  readonly needs?: readonly Capability[];
}

export interface ComposerSlotProps {
  readonly sessionId: string | null;
  /** The chat is a read-only share: an extension must not write. */
  readonly readOnly: boolean;
  readonly insert: (text: string) => void;
  readonly openSidePanel: (id: string) => void;
}

export interface ComposerActionEntry extends ChatSlotEntry {
  readonly component: ComponentType<ComposerSlotProps>;
}

export interface ComposerPickerEntry extends ChatSlotEntry {
  readonly label: LocalizedLabel;
  readonly icon: LucideIcon;
  /** Also offered under `/` as a local command of this name. */
  readonly command?: string;
  readonly onSelect: (ctx: ComposerSlotProps) => void;
}

/** Renders one typed message part. Core part types (`answers`, `suggestions`)
 *  are never handed to an extension. `part` is untrusted data. */
export interface MessagePartRenderer extends ChatSlotEntry {
  readonly type: string;
  readonly component: ComponentType<{ part: Record<string, unknown>; message: ChatMessageDTO }>;
}

export interface MessageActionEntry extends ChatSlotEntry {
  readonly component: ComponentType<{
    message: ChatMessageDTO;
    sessionId: string | null;
    readOnly: boolean;
  }>;
}

export interface ChatSidebarSectionEntry extends ChatSlotEntry {
  readonly component: ComponentType<{ agentId: string; onOpenSession: (id: string) => void }>;
}

export interface ChatHeaderActionEntry extends ChatSlotEntry {
  readonly component: ComponentType<{ sessionId: string | null; readOnly: boolean }>;
}

export interface SidePanelEntry extends ChatSlotEntry {
  readonly component: ComponentType<{ sessionId: string | null; onClose: () => void }>;
  readonly title: LocalizedLabel;
}

export interface GlobalProviderEntry extends ChatSlotEntry {
  readonly component: ComponentType<{ children: ReactNode }>;
}

export interface EditionChatContributions {
  readonly composerActions?: readonly ComposerActionEntry[];
  readonly composerPickers?: readonly ComposerPickerEntry[];
  readonly messageParts?: readonly MessagePartRenderer[];
  readonly messageActions?: readonly MessageActionEntry[];
  readonly chatSidebarSections?: readonly ChatSidebarSectionEntry[];
  readonly chatHeaderActions?: readonly ChatHeaderActionEntry[];
  readonly sidePanels?: readonly SidePanelEntry[];
  readonly globalProviders?: readonly GlobalProviderEntry[];
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

/** A card an edition shows at the top of the home page. */
export interface EditionHomeSection {
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

export interface CustomServerSourceProps {
  readonly mode: "create" | "edit";
  /** create: what this source handed back earlier (the person went back from the test step). */
  readonly initialDraft: CustomServerDraft | null;
  /** edit: the connection being edited. */
  readonly existing: HttpToolsSnapshot | null;
  /** create: base URL, login, tools, scopes, import metadata. edit: only tools, scopes and metadata are used. */
  readonly onComplete: (draft: CustomServerDraft) => void;
  readonly onCancel: () => void;
}

/** A way to describe a plain HTTP API's endpoints other than by hand. */
export interface CustomServerSource {
  readonly id: string;
  readonly label: LocalizedLabel;
  readonly description: LocalizedLabel;
  readonly icon: LucideIcon;
  readonly component: ComponentType<CustomServerSourceProps>;
  /** Capabilities the caller must all hold for the source to show. */
  readonly needs?: readonly Capability[];
  /** Whether "Edit endpoints" opens this source for an existing connection. */
  readonly canEdit?: (existing: HttpToolsSnapshot) => boolean;
}
