import { createFileRoute } from "@tanstack/react-router";
import { PanelRightClose, PanelRightOpen } from "lucide-react";
import { useState } from "react";
import { ChatSessionPicker, ChatWindow } from "@/components/chat-window";
import { CopilotAvatar, CopilotPersonaHeader } from "@/components/copilot-persona";
import { CopilotWorkPanel } from "@/components/copilot-work-panel";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { useIsMobile } from "@/hooks/use-mobile";
import { mascotState } from "@/lib/copilot-status";
import { useMay } from "@/lib/governance-hooks";
import { useChatSessions } from "@/lib/hooks-chat";
import { useCopilotProfile } from "@/lib/hooks-copilot";
import { useT } from "@/lib/i18n";

export const Route = createFileRoute("/copilot")({ component: CopilotPage });

export function CopilotPage() {
  const t = useT();
  const may = useMay();
  const { data: profile } = useCopilotProfile();
  const agentId = profile?.agentId ?? "";
  const { data: sessions = [] } = useChatSessions(agentId || undefined);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [panelOpen, setPanelOpen] = useState(true);
  const mobile = useIsMobile();
  if (!may("copilot:use")) return null; // nav is the main gate; defense in depth
  const name = profile?.displayName ?? "Copilot";
  const chat = agentId ? (
    <ChatWindow
      agentId={agentId}
      agentName={name}
      hideHeader
      className="h-full min-h-0 flex-1 rounded-none border-0"
      sessionId={sessionId}
      onSessionChange={setSessionId}
      promptStarters={[
        t("Keep an eye on something for me", "Behalte etwas für mich im Blick"),
        t("What are you working on for me?", "Woran arbeitest du gerade für mich?"),
        t("What is waiting on me?", "Was wartet auf mich?"),
      ]}
      emptyIntro={
        <div className="mb-3 flex flex-col items-center gap-2">
          <CopilotAvatar
            avatar={profile?.avatar ?? { shape: "round", color: "indigo" }}
            state={mascotState(profile?.status ?? "ready")}
            size={64}
          />
          <p className="text-sm text-foreground">
            {t(
              `I'm ${name}. Tell me what to keep an eye on and I'll take it from there.`,
              `Ich bin ${name}. Sag mir, was ich im Blick behalten soll, den Rest übernehme ich.`,
            )}
          </p>
        </div>
      }
    />
  ) : null;
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
