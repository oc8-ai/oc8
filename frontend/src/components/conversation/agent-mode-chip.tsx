// The chat is in Agent mode (Copilot Agent Builder §2): the Copilot is setting
// up a new agent with the member. ✕ leaves the mode; the chat stays.
import { Bot, X } from "lucide-react";

import { useT } from "@/lib/i18n";

export function AgentModeChip({ onLeave, busy = false }: { onLeave: () => void; busy?: boolean }) {
  const t = useT();
  return (
    <div className="inline-flex min-h-9 items-center gap-2 rounded-full border border-border bg-card pl-3 pr-1 text-sm">
      <Bot className="size-4 text-primary" aria-hidden />
      <span>{t("Setting up an agent", "Agent anlegen")}</span>
      <button
        type="button"
        onClick={onLeave}
        disabled={busy}
        aria-label={t("Leave agent mode", "Agent-Modus verlassen")}
        className="grid size-9 place-items-center rounded-full text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
      >
        <X className="size-3.5" aria-hidden />
      </button>
    </div>
  );
}
