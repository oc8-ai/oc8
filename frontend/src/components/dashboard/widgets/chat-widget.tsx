// One grid tile bound to its own chat session. It mounts the same
// `Conversation` as the Copilot page and the dock (spec §2.1); draft text is
// scoped to this component's own state rather than a shared tabs manager.
import { useState } from "react";
import { urlId } from "@/lib/ids";
import { Plus, WifiOff } from "lucide-react";
import { Conversation } from "@/components/conversation";
import { copilotStarters } from "@/components/conversation/copilot-starters";
import { PendingProposals } from "@/components/copilot-dock";
import { SessionPicker } from "@/components/conversation/session-picker";
import { useCan } from "@/lib/governance-hooks";
import { useAssistant } from "@/lib/hooks";
import { useChatSessions } from "@/lib/hooks-chat";
import { useLiveConnectionStatus } from "@/lib/live/provider";
import { useT } from "@/lib/i18n";

// Gated the same way `CopilotDock` gates itself (copilot-dock.tsx) and for
// the same reason: checked BEFORE `useAssistant()`/`useChatSessions()` ever
// mount, not inside a later `if`, so a member without `copilot:use` never
// fires a GET /assistant + GET /chat/sessions the backend would 403 anyway.
// The backend already refuses those for such a member (this is UX, not a
// security boundary) -- without this gate, the composer below still
// renders and accepts input, but silently no-ops on send because
// `assistantAgentId` never resolves. Reuses `CopilotDock`'s own
// `PendingProposals` and reconnect banner for full surface parity with the
// floating dock.
export function ChatWidget({
  config,
  onConfigChange,
}: {
  config: Record<string, unknown>;
  onConfigChange: (config: Record<string, unknown>) => void;
}) {
  const t = useT();
  const can = useCan();
  if (!can("copilot:use")) {
    return (
      <div className="flex h-full items-center justify-center p-3 text-center text-xs text-muted-foreground">
        {t(
          "You don't have access to Copilot chat.",
          "Du hast keinen Zugriff auf den Copilot-Chat.",
        )}
      </div>
    );
  }
  return <ChatWidgetPanel config={config} onConfigChange={onConfigChange} />;
}

function ChatWidgetPanel({
  config,
  onConfigChange,
}: {
  config: Record<string, unknown>;
  onConfigChange: (config: Record<string, unknown>) => void;
}) {
  const t = useT();
  const de = t("en", "de") === "de";
  const [draft, setDraft] = useState("");
  const { data: assistant } = useAssistant();
  const assistantAgentId = assistant?.agentId;
  const { data: sessions } = useChatSessions(assistantAgentId);
  const liveConnectionStatus = useLiveConnectionStatus();
  // Stored with the dashboard and put into API paths: only a UUID is taken.
  const sessionId = urlId(config.sessionId) ?? null;

  function setSessionId(id: string | null) {
    onConfigChange({ ...config, sessionId: id });
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-1 border-b border-border px-2 py-1">
        {assistantAgentId && (
          <button
            type="button"
            onClick={() => setSessionId(null)}
            aria-label={t("New chat", "Neuer Chat")}
            title={t("New chat", "Neuer Chat")}
            className="grid size-9 shrink-0 place-items-center rounded-md text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
          >
            <Plus className="h-4 w-4" />
          </button>
        )}
        {assistantAgentId && sessions && sessions.length > 0 && (
          <SessionPicker sessions={sessions} sessionId={sessionId} onSelect={setSessionId} />
        )}
      </div>
      {liveConnectionStatus === "disconnected" && (
        <div className="flex items-center gap-1.5 border-b border-status-warning/30 bg-status-warning/10 px-3 py-1 text-xs text-status-warning">
          <WifiOff className="h-3.5 w-3.5" />
          <span>
            {t(
              "Reconnecting -- new messages may be delayed.",
              "Verbindung wird wiederhergestellt -- neue Nachrichten können sich verzögern.",
            )}
          </span>
        </div>
      )}
      <PendingProposals de={de} />
      <div className="flex flex-1 flex-col overflow-hidden">
        {assistantAgentId && (
          <Conversation
            agentId={assistantAgentId}
            sessionId={sessionId}
            onSessionChange={setSessionId}
            variant="compact"
            draft={draft}
            onDraftChange={setDraft}
            placeholder={t("Ask your Copilot …", "Frag deinen Copilot …")}
            emptyState={
              <p className="py-4 text-sm text-muted-foreground">
                {t(
                  "No chat here yet. Ask your Copilot something, or start with one of these.",
                  "Hier gibt es noch keinen Chat. Frag deinen Copilot etwas oder fang mit einem davon an.",
                )}
              </p>
            }
            promptStarters={copilotStarters(t)}
          />
        )}
      </div>
    </div>
  );
}
