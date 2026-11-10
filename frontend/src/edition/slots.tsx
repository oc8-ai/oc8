import { Component, createContext, useContext, useState, type ReactNode } from "react";

import type { ChatMessageDTO } from "@/lib/hooks-chat";

import { useChatSlot, useFrontendEdition, type ComposedChat } from "./composition";
import type { EditionSidebarSlotProps } from "./contracts";

/** Part types core renders itself; an extension never gets them. */
const CORE_PART_TYPES = new Set(["answers", "suggestions", "agent_builder"]);

/** A contributed component that throws renders nothing (or `fallback`) and must not
 *  take the chat down with it. */
export class SlotBoundary extends Component<
  { id: string; children: ReactNode; fallback?: ReactNode; resetKey?: string | null },
  { failed: boolean }
> {
  state = { failed: false };

  componentDidUpdate(prev: { resetKey?: string | null }) {
    // A failure belongs to the chat it happened in: another chat tries again.
    if (this.state.failed && prev.resetKey !== this.props.resetKey)
      this.setState({ failed: false });
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: unknown) {
    console.error(`Edition chat slot "${this.props.id}" failed`, error);
  }

  render() {
    return this.state.failed ? (this.props.fallback ?? null) : this.props.children;
  }
}

/** The edition's sidebar header, if one supplies it. */
export function EditionSidebarHeader({ collapsed }: EditionSidebarSlotProps) {
  const { sidebarHeader: Header } = useFrontendEdition();
  return Header ? <Header collapsed={collapsed} /> : null;
}

/** The edition's sidebar footer, if one supplies it. */
export function EditionSidebarFooter({ collapsed }: EditionSidebarSlotProps) {
  const { sidebarFooter: Footer } = useFrontendEdition();
  return Footer ? <Footer collapsed={collapsed} /> : null;
}

/** Panels editions append to the profile page, in extension order. */
export function EditionProfileSections() {
  const { profileSections } = useFrontendEdition();
  return (
    <>
      {profileSections.map(({ id, component: Section }) => (
        <Section key={id} />
      ))}
    </>
  );
}

/** Typed message parts, each drawn by the edition renderer registered for its
 *  type. Unknown types render nothing; core part types are never delegated. */
export function MessageParts({
  message,
  renderers,
}: {
  message: ChatMessageDTO;
  renderers: ComposedChat["messageParts"];
}) {
  const parts = message.parts ?? [];
  return (
    <>
      {parts.map((part, i) => {
        if (typeof part.type !== "string" || CORE_PART_TYPES.has(part.type)) return null;
        const renderer = renderers.find((r) => r.type === part.type);
        if (!renderer) return null;
        const Part = renderer.component;
        return (
          <SlotBoundary key={`${renderer.id}:${i}`} id={renderer.id}>
            <Part part={part} message={message} />
          </SlotBoundary>
        );
      })}
    </>
  );
}

/** Edition actions after a message's own action row. */
export function MessageActions({
  message,
  sessionId,
  readOnly,
}: {
  message: ChatMessageDTO;
  sessionId: string | null;
  readOnly: boolean;
}) {
  const actions = useChatSlot("messageActions");
  return (
    <>
      {actions.map(({ id, component: Action }) => (
        <SlotBoundary key={id} id={id}>
          <Action message={message} sessionId={sessionId} readOnly={readOnly} />
        </SlotBoundary>
      ))}
    </>
  );
}

/** Edition actions in the Copilot page header. */
export function ChatHeaderActions({
  sessionId,
  readOnly,
}: {
  sessionId: string | null;
  readOnly: boolean;
}) {
  const actions = useChatSlot("chatHeaderActions");
  return (
    <>
      {actions.map(({ id, component: Action }) => (
        <SlotBoundary key={id} id={id} resetKey={sessionId}>
          <Action sessionId={sessionId} readOnly={readOnly} />
        </SlotBoundary>
      ))}
    </>
  );
}

/** Errors thrown by the app subtree under a provider: not the provider's. */
const appErrors = new WeakSet<object>();

/** Marks errors from `children` as the app's own and passes them up. */
class AppSubtree extends Component<{ children: ReactNode }, { error: unknown }> {
  state = { error: null as unknown };

  static getDerivedStateFromError(error: unknown) {
    if (typeof error === "object" && error !== null) appErrors.add(error);
    return { error };
  }

  render() {
    if (this.state.error !== null) throw this.state.error;
    return this.props.children;
  }
}

/** Catches only a provider's own failure; an error from the app below it is
 *  rethrown untouched, so it is neither swallowed nor logged as a slot failure. */
class ProviderBoundary extends Component<
  { id: string; fallback: ReactNode; children: ReactNode },
  { error: unknown }
> {
  state = { error: null as unknown };

  static getDerivedStateFromError(error: unknown) {
    return { error: error ?? new Error("provider failed") };
  }

  componentDidCatch(error: unknown) {
    if (typeof error === "object" && error !== null && appErrors.has(error)) return;
    console.error(`Edition chat slot "${this.props.id}" failed`, error);
  }

  render() {
    const { error } = this.state;
    if (error === null) return this.props.children;
    if (typeof error === "object" && appErrors.has(error)) throw error;
    return this.props.fallback;
  }
}

/** Wraps the app in the edition's global providers, first one outermost.
 *  A provider that throws is skipped. Extension contexts must therefore be
 *  optional: a consumer has to tolerate its provider being missing. */
export function ChatSlotProviders({ children }: { children: ReactNode }) {
  const providers = useChatSlot("globalProviders");
  return providers.reduceRight<ReactNode>((inner, { id, component: Provider }) => {
    const subtree = <AppSubtree>{inner}</AppSubtree>;
    return (
      <ProviderBoundary key={id} id={id} fallback={inner}>
        <Provider>{subtree}</Provider>
      </ProviderBoundary>
    );
  }, children);
}

interface SidePanelState {
  openId: string | null;
  open: (id: string) => void;
  close: () => void;
}

const SidePanelContext = createContext<SidePanelState | null>(null);
const CLOSED: SidePanelState = { openId: null, open: () => undefined, close: () => undefined };

function OwnSidePanelState({ children }: { children: ReactNode }) {
  const [openId, setOpenId] = useState<string | null>(null);
  return (
    <SidePanelContext.Provider value={{ openId, open: setOpenId, close: () => setOpenId(null) }}>
      {children}
    </SidePanelContext.Provider>
  );
}

/** Holds which side panel is open. An outer provider (the Copilot page, which
 *  renders the host beside the conversation) wins over a nested one. */
export function SidePanelProvider({ children }: { children: ReactNode }) {
  return useContext(SidePanelContext) ? (
    <>{children}</>
  ) : (
    <OwnSidePanelState>{children}</OwnSidePanelState>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useSidePanel(): SidePanelState {
  return useContext(SidePanelContext) ?? CLOSED;
}

/** The open side panel, right of the conversation (full variant only). */
export function SidePanelHost({ sessionId }: { sessionId: string | null }) {
  const panels = useChatSlot("sidePanels");
  const { openId, close } = useSidePanel();
  // Another chat starts with its panel closed.
  const [seenSession, setSeenSession] = useState(sessionId);
  if (seenSession !== sessionId) {
    setSeenSession(sessionId);
    if (openId !== null) close();
  }
  const panel = panels.find((p) => p.id === openId);
  if (!panel) return null;
  const Panel = panel.component;
  return (
    <aside className="hidden w-[420px] shrink-0 border-l border-border md:flex md:flex-col">
      <SlotBoundary id={panel.id} resetKey={sessionId}>
        <Panel sessionId={sessionId} onClose={close} />
      </SlotBoundary>
    </aside>
  );
}

/** Cards editions show at the top of the home page, in extension order. */
export function EditionHomeSections() {
  const { homeSections } = useFrontendEdition();
  return (
    <>
      {homeSections.map(({ id, component: Section }) => (
        <Section key={id} />
      ))}
    </>
  );
}
