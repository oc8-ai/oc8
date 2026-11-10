import { createFileRoute, useNavigate, useRouterState } from "@tanstack/react-router";
import { PanelRightClose, PanelRightOpen } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { ChatHeaderActions, SidePanelHost, SidePanelProvider } from "@/edition/slots";
import { Conversation } from "@/components/conversation";
import { copilotStarters } from "@/components/conversation/copilot-starters";
import { CopilotSidebar } from "@/components/conversation/copilot-sidebar";
import { CopilotAvatar, CopilotPersonaHeader } from "@/components/copilot-persona";
import { CopilotWorkPanel } from "@/components/copilot-work-panel";
import { isPlainClick } from "@/components/conversation/object-links";
import { parsePin, useOpenPinnedChat } from "@/components/conversation/open-pinned-chat";
import { useObjectNavigation } from "@/components/conversation/use-object-navigation";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { useIsMobile } from "@/hooks/use-mobile";
import { ApiError } from "@/lib/api";
import { mascotState } from "@/lib/copilot-status";
import { useMay } from "@/lib/governance-hooks";
import { useChatSession } from "@/lib/conversation-api";
import { useAgent } from "@/lib/hooks";
import { useChatSessions } from "@/lib/hooks-chat";
import { useAgentIdeas } from "@/lib/agent-builder-api";
import { useCopilotProfile } from "@/lib/hooks-copilot";
import { useLang, useT } from "@/lib/i18n";
import { urlId } from "@/lib/ids";

export interface CopilotSearch {
  /** A conversation to open -- where an answer's session chip
   *  ("oc8://chat_session/<id>") lands. */
  session?: string;
  /** An object to pin to a new conversation, as "<kind>:<id>" -- "Discuss
   *  with Copilot" and the Live tab's "Continue" send it. Untrusted: kept
   *  here as text and checked by `parsePin` before anything is created. */
  pin?: string;
}

export const Route = createFileRoute("/copilot")({
  validateSearch: (search: Record<string, unknown>): CopilotSearch => {
    const session = urlId(search.session);
    const pin = typeof search.pin === "string" && search.pin.length <= 4200 ? search.pin : null;
    return {
      ...(session ? { session } : {}),
      ...(pin ? { pin } : {}),
    };
  },
  component: CopilotRoute,
});

function CopilotRoute() {
  const { session, pin } = Route.useSearch();
  const navigate = useNavigate();
  // Every navigation gets a fresh history key, so following the same link
  // again (after switching conversations) selects it again.
  const navKey = useRouterState({
    select: (s) => s.location.state.__TSR_key ?? s.location.state.key ?? s.location.href,
  });
  return (
    <CopilotPage
      initialSessionId={session ?? null}
      navKey={navKey}
      pin={pin}
      // The address follows the open chat, without a history entry per click.
      onSessionOpened={(next) => {
        void navigate({ to: "/copilot", search: next ? { session: next } : {}, replace: true });
      }}
      // So a reload does not create another chat.
      onPinHandled={(sessionId) => {
        // A link that also named a chat keeps it when the pin is refused.
        const keep = sessionId ?? session;
        void navigate({ to: "/copilot", search: keep ? { session: keep } : {}, replace: true });
      }}
    />
  );
}

/** A link named a conversation that is not the Copilot's: say whose it is
 *  and link there rather than opening it under the Copilot's name. */
function ForeignSessionNotice({ agentId }: { agentId: string }) {
  const t = useT();
  const { data: agent } = useAgent(agentId);
  const openObject = useObjectNavigation();
  const href = `/agents/${encodeURIComponent(agentId)}`;
  return (
    <p role="status" className="border-b border-border px-4 py-2 text-xs text-muted-foreground">
      {t("This chat belongs to ", "Dieser Chat gehört zu ")}
      <a
        href={href}
        onClick={(e) => {
          if (!openObject || !isPlainClick(e)) return;
          e.preventDefault();
          openObject(href);
        }}
        className="text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {agent?.name ?? t("another agent", "einem anderen Agenten")}
      </a>
      .
    </p>
  );
}

export function CopilotPage({
  initialSessionId = null,
  navKey,
  pin,
  onPinHandled,
  onSessionOpened,
}: {
  initialSessionId?: string | null;
  navKey?: string;
  /** The raw `?pin=` value: a new chat is opened with that object pinned. */
  pin?: string;
  /** The pin was handled (chat created, or the link refused): drop it from
   *  the address, selecting `sessionId` when there is one. */
  onPinHandled?: (sessionId: string | null) => void;
  /** The open chat changed (picked in the sidebar, deleted, or a new one
   *  created): the address is replaced with it, null for no chat. */
  onSessionOpened?: (sessionId: string | null) => void;
} = {}) {
  const t = useT();
  const may = useMay();
  const { data: profile } = useCopilotProfile();
  const agentId = profile?.agentId ?? "";
  const { data: sessions = [] } = useChatSessions(agentId || undefined);
  const [sessionId, setSessionId] = useState<string | null>(null);
  // A linked conversation opens here only if it is the Copilot's own: known
  // from its session list, else from the session itself.
  const listed = !!initialSessionId && sessions.some((s) => s.id === initialSessionId);
  const { data: linked, error: linkedError } = useChatSession(
    initialSessionId && !listed ? initialSessionId : null,
  );
  // The chat is gone or not this member's (404): say so, then a plain chat.
  const linkedGone = linkedError instanceof ApiError && linkedError.status === 404;
  const owner = listed ? agentId : (linked?.agentId ?? null);
  const ownLink = !!initialSessionId && !!agentId && owner === agentId;
  const foreignOwner = initialSessionId && owner && owner !== agentId ? owner : null;
  // The chat this page opened itself, until the address names it too. The
  // router hands out a navigation's new key before its search, so for one
  // render "Neuer Chat" looked like the old link followed again and reopened
  // the chat just left.
  const openedHere = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (openedHere.current !== undefined) {
      if ((initialSessionId ?? null) !== openedHere.current) return;
      openedHere.current = undefined;
    }
    if (ownLink && initialSessionId) setSessionId(initialSessionId);
  }, [ownLink, initialSessionId, navKey]);
  // A `?pin=` link: one new chat per link, however often this effect runs
  // (StrictMode, a second click on the same link). It only creates the chat
  // and pins the object; it never sends a message or starts a run.
  const openPinned = useOpenPinnedChat();
  const [pinNote, setPinNote] = useState<string | null>(null);
  const pinStarted = useRef<string | null>(null);
  const mayUse = may("copilot:use");
  // Once the member has left, a late reply must not select, note or navigate
  // (a navigate would pull them back here).
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    if (!pin) {
      pinStarted.current = null;
      return;
    }
    if (!mayUse || pinStarted.current === pin) return;
    pinStarted.current = pin;
    const parsed = parsePin(pin);
    if (!parsed) {
      setPinNote(
        t(
          "That link did not name an object I can pin, so this is a new chat.",
          "Dieser Link nennt kein Objekt, das ich anheften kann. Das hier ist ein neuer Chat.",
        ),
      );
      onPinHandled?.(null);
      return;
    }
    openPinned(parsed).then(
      ({ sessionId: opened, pinned }) => {
        if (!mounted.current) return;
        setSessionId(opened);
        setPinNote(
          pinned === "refused"
            ? t("You can't pin that", "Das kannst du nicht anheften")
            : pinned === "failed"
              ? t(
                  "The object was not pinned. Try pinning it from the + menu.",
                  "Das Objekt wurde nicht angeheftet. Versuch es über das Plus-Menü.",
                )
              : null,
        );
        onPinHandled?.(opened);
      },
      () => {
        if (!mounted.current) return;
        setPinNote(
          t(
            "The new chat could not be created. Try again.",
            "Der neue Chat konnte nicht angelegt werden. Versuch es noch einmal.",
          ),
        );
        onPinHandled?.(null);
      },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per pin value; the ref guards re-runs
  }, [pin, mayUse]);
  const [panelOpen, setPanelOpen] = useState(true);
  const openSession = (id: string | null) => {
    setSessionId(id);
    openedHere.current = id !== (initialSessionId ?? null) ? id : undefined;
    onSessionOpened?.(id);
  };
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const { data: current } = useChatSession(sessionId);
  const mobile = useIsMobile();
  // Agent ideas for this tenant's systems first (Copilot Agent Builder §3.6),
  // then the Copilot's own ways in.
  const { lang } = useLang();
  const { data: ideas = [] } = useAgentIdeas(may("copilot:use"));
  if (!may("copilot:use")) return null; // nav is the main gate; defense in depth
  const name = profile?.displayName ?? "Copilot";
  const pick = (text: { en: string; de: string }) => (lang === "de" ? text.de : text.en);
  const starters = [
    ...ideas.map((idea) => ({
      title: pick(idea.title),
      subtitle: pick(idea.subtitle),
      prompt: pick(idea.prompt),
    })),
    ...copilotStarters(t),
  ];
  const chat = agentId ? (
    <Conversation
      agentId={agentId}
      sessionId={sessionId}
      onSessionChange={openSession}
      variant="full"
      agentMode
      promptStarters={starters}
      emptyState={
        <div className="flex flex-col items-center gap-3 pt-12 pb-6 text-center">
          <CopilotAvatar
            avatar={profile?.avatar ?? { shape: "round", color: "indigo" }}
            state={mascotState(profile?.status ?? "ready")}
            size={64}
          />
          <p className="text-sm text-foreground">
            {t(
              "I'm {name}. Tell me what to keep an eye on and I'll take it from there.",
              "Ich bin {name}. Sag mir, was ich im Blick behalten soll, den Rest übernehme ich.",
            ).replace("{name}", name)}
          </p>
        </div>
      }
    />
  ) : (
    <div className="mx-auto w-full max-w-[760px] space-y-3 p-6" aria-busy="true">
      <Skeleton className="h-4 w-1/2 rounded-md" />
      <Skeleton className="h-4 w-2/3 rounded-md" />
    </div>
  );
  return (
    <SidePanelProvider>
      <div className="flex min-h-0 flex-1 flex-col sm:flex-row">
        {agentId && (
          <CopilotSidebar
            agentId={agentId}
            sessionId={sessionId}
            onSelect={openSession}
            collapsed={sidebarCollapsed}
            onToggle={() => setSidebarCollapsed((v) => !v)}
          />
        )}
        <section className="flex min-h-0 min-w-0 flex-1 flex-col border-r border-border">
          <div className="border-b border-border px-4 py-3">
            <CopilotPersonaHeader variant="page">
              <ChatHeaderActions sessionId={sessionId} readOnly={!!current?.readOnly} />
              {mobile ? (
                <Sheet>
                  <SheetTrigger asChild>
                    <Button variant="outline" size="sm">
                      {t("Work", "Arbeit")}
                      {profile?.status === "waiting" && profile.activeCount > 0 && (
                        // Waiting on you: the orange dot and count (DESIGN.md).
                        <span className="ml-1 inline-flex items-center gap-1 text-xs">
                          <span className="size-2 rounded-full bg-status-warning" aria-hidden />
                          {profile.activeCount}
                        </span>
                      )}
                    </Button>
                  </SheetTrigger>
                  <SheetContent side="bottom" className="h-[80vh] p-0">
                    <SheetTitle className="sr-only">{t("Work", "Arbeit")}</SheetTitle>
                    <CopilotWorkPanel />
                  </SheetContent>
                </Sheet>
              ) : (
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => setPanelOpen((o) => !o)}
                  aria-label={
                    panelOpen
                      ? t("Hide work panel", "Arbeitsbereich ausblenden")
                      : t("Show work panel", "Arbeitsbereich einblenden")
                  }
                >
                  {panelOpen ? (
                    <PanelRightClose className="h-4 w-4" />
                  ) : (
                    <PanelRightOpen className="h-4 w-4" />
                  )}
                </Button>
              )}
            </CopilotPersonaHeader>
          </div>
          {foreignOwner && <ForeignSessionNotice agentId={foreignOwner} />}
          {linkedGone && !pinNote && (
            <p
              role="status"
              className="border-b border-border px-4 py-2 text-xs text-muted-foreground"
            >
              {t(
                "That chat doesn't exist or isn't yours.",
                "Diesen Chat gibt es nicht oder er gehört nicht dir.",
              )}
            </p>
          )}
          {pinNote && (
            <p
              role="status"
              className="border-b border-border px-4 py-2 text-xs text-muted-foreground"
            >
              {pinNote}
            </p>
          )}
          {chat}
        </section>
        {!mobile && <SidePanelHost sessionId={sessionId} />}
        {!mobile && panelOpen && (
          <aside className="hidden w-[380px] shrink-0 md:flex md:flex-col">
            <CopilotWorkPanel />
          </aside>
        )}
      </div>
    </SidePanelProvider>
  );
}
