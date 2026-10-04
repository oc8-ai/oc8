// One grid tile bound to its own chat session -- unlike the floating dock's
// tabs, a grid tile never unmounts while the page is open, so `active` is
// always true and draft text is scoped to this component's own state
// rather than lifted to a shared tabs manager.
import { useState } from "react";
import { Plus, WifiOff } from "lucide-react";
import { CopilotChatTab, PendingProposals } from "@/components/copilot-dock";
import { ChatSessionPicker } from "@/components/chat-window";
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
  const sessionId = typeof config.sessionId === "string" ? config.sessionId : null;

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
            className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
          >
            <Plus className="h-4 w-4" />
          </button>
        )}
        {assistantAgentId && sessions && sessions.length > 0 && (
          <ChatSessionPicker
            agentId={assistantAgentId}
            sessions={sessions}
            sessionId={sessionId}
            onSelect={setSessionId}
          />
        )}
      </div>
      {liveConnectionStatus === "disconnected" && (
        <div className="flex items-center gap-1.5 border-b border-amber-500/30 bg-amber-500/10 px-3 py-1 text-xs text-amber-700 dark:text-amber-400">
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
        <CopilotChatTab
          active
          sessionId={sessionId}
          onSessionChange={setSessionId}
          agentId={assistantAgentId}
          isAssistant
          agents={[]}
          departments={[]}
          mayStartRuns={false}
          onAddressAgent={() => {}}
          draft={draft}
          onDraftChange={setDraft}
        />
      </div>
    </div>
  );
}
