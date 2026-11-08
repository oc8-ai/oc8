import { createFileRoute, useRouterState } from "@tanstack/react-router";
import { PanelRightClose, PanelRightOpen } from "lucide-react";
import { useEffect, useState } from "react";
import { ChatSessionPicker } from "@/components/chat-window";
import { Conversation } from "@/components/conversation";
import { CopilotAvatar, CopilotPersonaHeader } from "@/components/copilot-persona";
import { CopilotWorkPanel } from "@/components/copilot-work-panel";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { useIsMobile } from "@/hooks/use-mobile";
import { mascotState } from "@/lib/copilot-status";
import { useMay } from "@/lib/governance-hooks";
import { isPlainClick } from "@/components/conversation/object-links";
import { useObjectNavigation } from "@/components/conversation/use-object-navigation";
import { useChatSession } from "@/lib/conversation-api";
import { useAgent } from "@/lib/hooks";
import { useChatSessions } from "@/lib/hooks-chat";
import { useCopilotProfile } from "@/lib/hooks-copilot";
import { isUuid, urlId } from "@/lib/ids";
import { useT } from "@/lib/i18n";

export interface CopilotSearch {
  /** A conversation to open -- where an answer's session chip
   *  ("oc8://chat_session/<id>") lands. */
  session?: string;
  /** An object to pin to a new conversation, as "<kind>:<id>" -- the Live
   *  tab's "Continue" sends "run:<id>". Accepted here so the link keeps it;
   *  the Copilot pins it once C3 pinning lands (Task 37). */
  pin?: string;
}

export const Route = createFileRoute("/copilot")({
  validateSearch: (search: Record<string, unknown>): CopilotSearch => {
    const session = urlId(search.session);
    const pin = typeof search.pin === "string" ? /^([a-z_]+):(.+)$/.exec(search.pin) : null;
    return {
      ...(session ? { session } : {}),
      ...(pin && isUuid(pin[2]) ? { pin: `${pin[1]}:${pin[2]}` } : {}),
    };
  },
  component: CopilotRoute,
});

function CopilotRoute() {
  const { session } = Route.useSearch();
  // Every navigation gets a fresh history key, so following the same link
  // again (after switching conversations) selects it again.
  const navKey = useRouterState({
    select: (s) => s.location.state.__TSR_key ?? s.location.state.key ?? s.location.href,
  });
  return <CopilotPage initialSessionId={session ?? null} navKey={navKey} />;
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
      {t("This conversation belongs to ", "Dieses Gespräch gehört zu ")}
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
}: { initialSessionId?: string | null; navKey?: string } = {}) {
  const t = useT();
  const may = useMay();
  const { data: profile } = useCopilotProfile();
  const agentId = profile?.agentId ?? "";
  const { data: sessions = [] } = useChatSessions(agentId || undefined);
  const [sessionId, setSessionId] = useState<string | null>(null);
  // A linked conversation opens here only if it is the Copilot's own: known
  // from its session list, else from the session itself.
  const listed = !!initialSessionId && sessions.some((s) => s.id === initialSessionId);
  const { data: linked } = useChatSession(initialSessionId && !listed ? initialSessionId : null);
  const owner = listed ? agentId : (linked?.agentId ?? null);
  const ownLink = !!initialSessionId && !!agentId && owner === agentId;
  const foreignOwner = initialSessionId && owner && owner !== agentId ? owner : null;
  useEffect(() => {
    if (ownLink && initialSessionId) setSessionId(initialSessionId);
  }, [ownLink, initialSessionId, navKey]);
  const [panelOpen, setPanelOpen] = useState(true);
  const mobile = useIsMobile();
  if (!may("copilot:use")) return null; // nav is the main gate; defense in depth
  const name = profile?.displayName ?? "Copilot";
  const starters = [
    t("Keep an eye on something for me", "Behalte etwas für mich im Blick"),
    t("What are you working on for me?", "Woran arbeitest du gerade für mich?"),
    t("What is waiting on me?", "Was wartet auf mich?"),
  ];
  const chat = agentId ? (
    <Conversation
      agentId={agentId}
      sessionId={sessionId}
      onSessionChange={setSessionId}
      variant="full"
      emptyState={(send) => (
        <div className="flex flex-col items-center gap-3 py-12 text-center">
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
          <div className="flex flex-wrap justify-center gap-2">
            {starters.map((starter) => (
              <button
                key={starter}
                type="button"
                onClick={() => send(starter)}
                className="rounded-full border border-border px-3 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {starter}
              </button>
            ))}
          </div>
        </div>
      )}
    />
  ) : (
    <div className="mx-auto w-full max-w-[760px] space-y-3 p-6" aria-busy="true">
      <Skeleton className="h-4 w-1/2 rounded-md" />
      <Skeleton className="h-4 w-2/3 rounded-md" />
    </div>
  );
  return (
    <div className="flex min-h-0 flex-1">
      <section className="flex min-w-0 flex-1 flex-col border-r border-border">
        <div className="border-b border-border px-4 py-3">
          <CopilotPersonaHeader variant="page">
            <ChatSessionPicker
              agentId={agentId}
              sessions={sessions}
              sessionId={sessionId}
              onSelect={setSessionId}
            />
            {mobile ? (
              <Sheet>
                <SheetTrigger asChild>
                  <Button variant="outline" size="sm">
                    {t("Work", "Arbeit")}
                    {profile?.status === "waiting" && profile.activeCount > 0 && (
                      <span className="ml-1 rounded-full bg-primary px-1.5 text-xs text-primary-foreground">
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
        {chat}
      </section>
      {!mobile && panelOpen && (
        <aside className="hidden w-[380px] shrink-0 md:flex md:flex-col">
          <CopilotWorkPanel />
        </aside>
      )}
    </div>
  );
}
