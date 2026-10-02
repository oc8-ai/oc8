// The composer's slash commands (§5.2 of the AI workplace design).
//
// The AUTHORITY on what a command does is the backend (`oc8/chat/modes.py`):
// the command word stays in the message text, the server parses it, records the
// mode on the turn and narrows what the run may do. This module only decides
// what the composer SHOWS -- which commands are in the picker, and whether the
// current draft is already in a mode. Its regex is a deliberate mirror of the
// backend's, and `chat-commands.test.ts` lists the exact cases both must agree
// on.
//
// One command is answered entirely here: `/budget`. The frontend already holds
// that number (GET /budgets/status), and spending an agent run to print it
// would be theatre.

import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface ChatModeDTO {
  key: string;
  /** English, from the backend's own registry. Translated below when we know
   *  the key; shown as-is when we do not, so a mode added server-side is
   *  readable without a frontend release. */
  summary: string;
  allowsTools: boolean;
  allowsWrites: boolean;
}

export interface ChatCommand {
  key: string;
  summary: string;
  /** Answered by the frontend; never sent as a message. */
  local: boolean;
  /** Permission the caller must hold for this to be offered, or null. */
  requires: string | null;
}

/** Commands that never become a run. Kept tiny on purpose: every entry here is
 *  a thing the composer can do that the audit trail will not see. */
export const LOCAL_COMMANDS: ChatCommand[] = [
  {
    key: "budget",
    summary: "Show spend so far and what is left.",
    local: true,
    // GET /budgets/status is budget:view-gated -- offering this to somebody who
    // would be 403'd is the picker lying to them.
    requires: "budget:view",
  },
];

export function useChatModes() {
  return useQuery({
    queryKey: ["chat", "modes"] as const,
    queryFn: () => api.get<ChatModeDTO[]>("/chat/modes"),
    // A compiled-in registry; it cannot change under a session.
    staleTime: Infinity,
  });
}

/** Every command this caller may use, in picker order: the server's modes
 *  first, then the local ones. */
export function chatCommands(
  modes: ChatModeDTO[] | undefined,
  can: (permission: string) => boolean,
): ChatCommand[] {
  const remote: ChatCommand[] = (modes ?? []).map((mode) => ({
    key: mode.key,
    summary: mode.summary,
    local: false,
    requires: null,
  }));
  return [...remote, ...LOCAL_COMMANDS.filter((c) => !c.requires || can(c.requires))];
}

/** The German half of a known command's description. A key we do not know
 *  falls back to the server's English -- see `ChatModeDTO.summary`. */
export function commandSummary(
  key: string,
  fallback: string,
  t: (en: string, de?: string) => string,
): string {
  switch (key) {
    case "ask":
      return t(
        "Answer from what you already know. No tools, no systems touched.",
        "Antwortet aus dem, was schon bekannt ist — ohne Werkzeuge, ohne Systemzugriff.",
      );
    case "plan":
      return t(
        "Work out the steps and show them. Changes nothing.",
        "Erarbeitet die Schritte und zeigt sie. Ändert nichts.",
      );
    case "do":
      return t(
        "Carry out what was agreed, under the usual guardrails.",
        "Führt das Vereinbarte aus — mit den gewohnten Guardrails.",
      );
    case "summarise":
      return t(
        "Condense this conversation into a decision record.",
        "Fasst dieses Gespräch zu einem Entscheidungsprotokoll zusammen.",
      );
    case "budget":
      return t("Show spend so far and what is left.", "Zeigt die bisherigen Kosten und den Rest.");
    default:
      return fallback;
  }
}

// Mirrors `oc8/chat/modes.py::_LEADING`. The `\s+` is what keeps
// "/etc/passwd is world readable?" a question rather than a command.
const WITH_BODY = /^\/([a-z][a-z0-9-]*)\s+([\s\S]*)$/;
const BARE = /^\/([a-z][a-z0-9-]*)\s*$/;

/** The command this draft is in, or null. */
export function detectCommand(
  text: string,
  commands: ChatCommand[],
): { command: ChatCommand; body: string } | null {
  const bare = BARE.exec(text);
  if (bare) {
    // A bare word is only a command when it needs no body -- i.e. a local one.
    // "/ask" on its own is a literal message, exactly as the backend reads it.
    const local = commands.find((c) => c.key === bare[1] && c.local);
    return local ? { command: local, body: "" } : null;
  }
  const matched = WITH_BODY.exec(text);
  if (!matched) return null;
  const command = commands.find((c) => c.key === matched[1]);
  if (!command) return null;
  const body = matched[2].trim();
  if (!body) return null;
  return { command, body };
}

/** The picker's filtered list while the reader types after a `/`. */
export function matchCommands(query: string, commands: ChatCommand[]): ChatCommand[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return commands;
  return commands.filter((c) => c.key.startsWith(needle));
}
