// Legacy: the dock's per-tab chat before the one Conversation (spec §2.1)
// replaced it. Nothing mounts this any more; Task 44 deletes it together
// with ChatWindow.
import { BookOpen, Send, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { ChatMarkdown } from "@/components/chat-markdown";
import { CopilotRunActivity, CopilotStreamingAnswer } from "@/components/copilot-run-activity";
import { RunComponents } from "@/components/run-record-card";
import {
  ComposerHint,
  SigilPopover,
  activeSigil,
  cycleIndex,
  replaceSigil,
  type SigilItem,
  type SigilToken,
} from "@/components/composer-sigils";
import { useT } from "@/lib/i18n";
import { useCan } from "@/lib/governance-hooks";
import {
  chatCommands,
  commandSummary,
  detectCommand,
  matchCommands,
  useChatModes,
} from "@/lib/chat-commands";
import type { Agent, Department } from "@/lib/mock-data";
import { useApprovals, useBudgetStatus, useKnowledgeBases } from "@/lib/hooks";
import { nextStepChips } from "@/lib/chat-suggestions";
import {
  useCreateChatSession,
  useChatMessages,
  useCopilotRunActivity,
  useSendChatMessage,
} from "@/lib/hooks-chat";

function greeting(de: boolean): string {
  return de
    ? "Hi, ich bin der oc8 Copilot. Ich kenne deine Agenten, Abteilungen und Guardrails — frag mich alles oder lass mich etwas konfigurieren."
    : "Hi, I'm the oc8 copilot. I know your agents, departments and guardrails — ask me anything or let me configure something.";
}

function unavailableText(de: boolean): string {
  return de
    ? "Der Copilot ist gerade nicht erreichbar. Versuch es gleich noch einmal."
    : "The copilot is unavailable right now. Try again in a moment.";
}

// One tab's entire single-session state machine -- the dock's whole body
// before Task 16, extracted unchanged: `input`, `sendError`, `pendingSend`,
// the lazy-create-on-send flow, and the full message-list/composer render
// tree. `sessionId`/`onSessionChange` replace what used to be this
// component's own `useState`, so the session lives in the parent's `tabs`
// array instead -- everything else behaves exactly as it did as a single
// dock instance.
//
// `active` doesn't gate any of the hooks above -- only the JSX this returns.
// An inactive tab keeps its own `useChatMessages` poll running (so a
// background tab's transcript doesn't go stale while unfocused, the same way
// a real multi-tab chat client behaves) and keeps its component instance
// (and therefore its `input` draft) alive across a tab switch; it just
// renders nothing while another tab is focused, which is what keeps two
// tabs' composers/messages from both landing in the DOM at once.
export function CopilotChatTab({
  sessionId,
  onSessionChange,
  agentId,
  isAssistant,
  agents,
  departments,
  mayStartRuns,
  onAddressAgent,
  active,
  draft,
  onDraftChange,
}: {
  sessionId: string | null;
  onSessionChange: (sessionId: string | null) => void;
  agentId: string | undefined;
  isAssistant: boolean;
  agents: Agent[];
  departments: Department[];
  mayStartRuns: boolean;
  onAddressAgent: (agentId: string) => void;
  active: boolean;
  draft: string;
  onDraftChange: (text: string) => void;
}) {
  const t = useT();
  const de = t("en", "de") === "de";
  const [input, setInputState] = useState(() => draft);
  function setInput(text: string) {
    setInputState(text);
    onDraftChange(text);
  }
  const scroller = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const can = useCan();
  const { data: modes } = useChatModes();
  const commands = chatCommands(modes, can);
  const [caret, setCaret] = useState(0);
  const [pickerIndex, setPickerIndex] = useState(0);
  const [contextRefs, setContextRefs] = useState<{ id: string; label: string }[]>([]);
  const sigil: SigilToken | null = activeSigil(input, caret);
  // Only fetched while the `#` picker is actually open: a knowledge-base list
  // is a paged query and nobody needs it for every chat render.
  const { data: bases } = useKnowledgeBases({
    search: sigil?.kind === "#" ? sigil.query : undefined,
    pageSize: 8,
  });
  const armed = detectCommand(input, commands);

  // Moved ahead of its original spot (just above `useCreateChatSession`) so
  // `hasMessages` is known before the budget-status gate right below needs it.
  const { data: messages } = useChatMessages(sessionId);
  const hasMessages = !!messages && messages.length > 0;

  // `/budget` is answered HERE, from the number the screen can already read --
  // no run, no tokens, no audit entry, because nothing happened. The command is
  // only offered when the caller holds budget:view (see LOCAL_COMMANDS).
  const [showBudget, setShowBudget] = useState(false);
  // Fetched once a conversation is actually active (not merely `showBudget`,
  // i.e. `/budget` typed), because `nextStepChips` below needs to know
  // `budget?.softExceeded` before the reader ever types that command -- but
  // only while `hasMessages` (an empty transcript never renders that chip
  // anyway), so an idle empty tab still never polls this.
  const { data: budget } = useBudgetStatus(null, { enabled: showBudget || hasMessages });
  // The approvals count feeds the same chip row.
  const { data: pendingApprovals } = useApprovals("pending");

  // The dock's `@` picker, grouped by department -- this is the front door, so
  // switching who you are talking to belongs here (§5.4).
  const agentItems: SigilItem[] = agents
    .filter((a) => !a.deletedAt)
    .filter((a) => a.name.toLowerCase().includes((sigil?.query ?? "").toLowerCase()))
    .slice(0, 8)
    .map((a) => ({
      id: a.id,
      label: a.name,
      hint:
        [a.role, departments.find((d) => d.id === a.departmentId)?.name]
          .filter(Boolean)
          .join(" · ") || undefined,
      // Shown but not choosable, with the reason: POST
      // /chat/sessions/{id}/messages needs run:start for any agent that is not
      // the assistant, so offering it silently would end in a 403.
      blocked:
        !mayStartRuns && a.id !== agentId
          ? t(
              "You may not start runs for another agent.",
              "Du darfst für andere Agenten keine Läufe starten.",
            )
          : undefined,
    }));

  const sigilItems: SigilItem[] =
    sigil?.kind === "/"
      ? matchCommands(sigil.query, commands).map((c) => ({
          id: c.key,
          label: `/${c.key}`,
          hint: commandSummary(c.key, c.summary, t),
        }))
      : sigil?.kind === "@"
        ? agentItems
        : sigil?.kind === "#"
          ? (bases?.items ?? [])
              .filter((kb) => !contextRefs.some((ref) => ref.id === kb.id))
              .map((kb) => ({ id: kb.id, label: kb.name, hint: kb.description || undefined }))
          : [];

  function pickSigilItem(item: SigilItem) {
    if (!sigil) return;
    if (sigil.kind === "/") {
      // The command STAYS in the text: the backend is the parser, and a mode
      // held only in React state would be lost by any other door.
      setInput(replaceSigil(input, sigil, `/${item.id.replace(/^\//, "")} `));
    } else if (sigil.kind === "@") {
      onAddressAgent(item.id);
      setInput(replaceSigil(input, sigil, ""));
      setPickerIndex(0);
      return;
    } else {
      setContextRefs((prev) => [...prev, { id: item.id, label: item.label }]);
      setInput(replaceSigil(input, sigil, ""));
    }
    setPickerIndex(0);
    inputRef.current?.focus();
  }

  const createSession = useCreateChatSession();

  const sendMessage = useSendChatMessage(sessionId ?? "");

  // A send that failed (session creation OR the message post itself): shown
  // as a transcript bubble, same as the pre-rewrite dock's own
  // "Der Copilot ist gerade nicht erreichbar..." notice, since a chat
  // surface losing a message silently with no toast and no trace is worse
  // here than elsewhere -- the reader has no other way to tell their message
  // never went anywhere.
  const [sendError, setSendError] = useState(false);

  function fail(text: string) {
    setSendError(true);
    // Put the text back so a retry doesn't mean retyping it.
    setInput(text);
  }

  // The run-activity strip's own tracking id -- set from the same-response
  // signal useSendChatMessage's onSuccess receives (the one place the real
  // run_id actually exists; the user's own persisted ChatMessage row never
  // gets one, only the assistant's terminal reply does), and reset before
  // every new send so a prior run's id can't leak into a new one. Deriving
  // this from `messages` instead (as before) broke the moment the 2s poll
  // replaced the cache with the persisted, runId-less user row.
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  // A message typed before any session exists yet: send() creates the
  // session first, then this fires once `sessionId` (and therefore a
  // `sendMessage` bound to the right session) lands on the next render.
  const [pendingSend, setPendingSend] = useState<{
    text: string;
    refs: { kind: string; id: string }[];
  } | null>(null);
  useEffect(() => {
    if (!sessionId || pendingSend === null) return;
    const { text, refs } = pendingSend;
    setPendingSend(null);
    sendMessage.mutate(
      { message: text, contextRefs: refs },
      {
        onSuccess: (message) => {
          setActiveRunId(message.runId);
          setContextRefs([]);
        },
        onError: () => fail(text),
      },
    );
    // sendMessage/fail are fresh every render; only sessionId/pendingSend
    // should re-trigger this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, pendingSend]);

  useEffect(() => {
    const el = scroller.current;
    if (el && typeof el.scrollTo === "function") {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    }
  }, [messages, sendError]);

  // Focus once, on mount -- a tab only ever mounts while the dock itself is
  // open (the parent nests every `CopilotChatTab` inside `{open && (...)}`),
  // so "just mounted" already means "the dock/tab just became visible", the
  // same moment the old single-session dock used to focus on `open` flipping
  // true.
  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Same "is the agent still typing" signal as ChatWindow: the transcript's
  // own last turn.
  const waitingOnAgent =
    !!messages && messages.length > 0 && messages[messages.length - 1].role === "user";
  const busy = createSession.isPending || sendMessage.isPending || waitingOnAgent;
  // Same ["run", runId] cache CopilotStreamingAnswer itself reads -- calling
  // the hook again here is a cache hit, not a second request, and is what
  // lets the typing dots above give way to that component the instant the
  // first "run.token_delta" fragment lands instead of staying dots for the
  // whole turn.
  const { data: activeRun } = useCopilotRunActivity(sessionId, activeRunId);
  const streamingAnswer = !!activeRun?.liveAnswer;

  function send() {
    const text = input.trim();
    if (!text || busy || !agentId) return;
    const local = detectCommand(text, commands);
    if (local?.command.local) {
      if (local.command.key === "budget") {
        setShowBudget(true);
        setInput("");
      }
      return;
    }
    const refs = contextRefs.map((ref) => ({ kind: "knowledge_base", id: ref.id }));
    setSendError(false);
    setInput("");
    setActiveRunId(null);
    if (!sessionId) {
      createSession.mutate(agentId, {
        onSuccess: (session) => {
          onSessionChange(session.id);
          setPendingSend({ text, refs });
        },
        onError: () => fail(text),
      });
      return;
    }
    sendMessage.mutate(
      { message: text, contextRefs: refs },
      {
        onSuccess: (message) => {
          setActiveRunId(message.runId);
          setContextRefs([]);
        },
        onError: () => fail(text),
      },
    );
  }

  // Deterministic, computed from state (§5.3) -- these three strings used to be
  // a fixed list shown only on an empty transcript, which meant the composer
  // had nothing to suggest at the exact moments it mattered most.
  const lastUserTurn = [...(messages ?? [])].reverse().find((m) => m.role === "user");
  const suggestions = nextStepChips(
    {
      hasMessages,
      lastTurnRole: messages?.length ? messages[messages.length - 1].role : null,
      lastUserMode: lastUserTurn?.mode ?? null,
      pendingApprovals: pendingApprovals?.length ?? 0,
      budgetSoftExceeded: !!budget?.softExceeded,
      mayViewBudget: can("budget:view"),
      promptStarters: agents.find((a) => a.id === agentId)?.promptStarters ?? [],
    },
    t,
  );

  if (!active) return null;

  return (
    <>
      <CopilotRunActivity sessionId={sessionId} runId={activeRunId} />
      <div ref={scroller} className="flex-1 space-y-3 overflow-y-auto px-4 py-4">
        {!hasMessages && (
          <div className="flex gap-2">
            <img
              src="/octopus_oc8.svg"
              alt=""
              className="mt-0.5 h-6 w-6 shrink-0"
              draggable={false}
            />
            <ChatMarkdown text={greeting(de)} className="max-w-[88%]" />
          </div>
        )}
        {messages?.map((m) =>
          m.role === "user" ? (
            <div key={m.id} className="flex justify-end">
              <div className="max-w-[85%] rounded-2xl rounded-br-sm bg-primary px-3 py-2 text-sm text-primary-foreground">
                {m.content}
              </div>
            </div>
          ) : (
            <div key={m.id} className="flex flex-col gap-1">
              <div className="flex gap-2">
                <img
                  src="/octopus_oc8.svg"
                  alt=""
                  className="mt-0.5 h-6 w-6 shrink-0"
                  draggable={false}
                />
                <ChatMarkdown text={m.content} className="max-w-[88%]" />
              </div>
              <RunComponents components={m.renderedComponents} className="ml-8 space-y-2" />
            </div>
          ),
        )}
        {waitingOnAgent &&
          (streamingAnswer ? (
            <CopilotStreamingAnswer sessionId={sessionId} runId={activeRunId} />
          ) : (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <img src="/octopus_oc8.svg" alt="" className="h-6 w-6 shrink-0" draggable={false} />
              <span className="inline-flex gap-1">
                {[0, 1, 2].map((i) => (
                  <span
                    key={i}
                    className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary"
                    style={{ animationDelay: `${i * 150}ms` }}
                  />
                ))}
              </span>
            </div>
          ))}
        {sendError && (
          <div className="flex gap-2">
            <img
              src="/octopus_oc8.svg"
              alt=""
              className="mt-0.5 h-6 w-6 shrink-0"
              draggable={false}
            />
            <div className="max-w-[88%] rounded-xl border border-border bg-background/40 px-3 py-2 text-sm text-muted-foreground">
              {unavailableText(de)}
            </div>
          </div>
        )}
      </div>

      {suggestions.length > 0 && (
        <div className="flex flex-wrap gap-1.5 px-4 pb-2">
          {suggestions.map((s) => (
            <button
              key={s.id}
              type="button"
              onClick={() => {
                setInput(s.insert);
                inputRef.current?.focus();
              }}
              className="rounded-full border border-border bg-background/40 px-2.5 py-1 text-[11px] text-muted-foreground transition hover:text-foreground"
            >
              {s.label}
            </button>
          ))}
        </div>
      )}

      {showBudget && (
        <div className="mx-4 mb-2 rounded-lg border border-border bg-background/40 px-3 py-2 text-xs">
          <div className="mb-1 flex items-center justify-between">
            <span className="font-medium">{t("Budget", "Budget")}</span>
            <button
              type="button"
              onClick={() => setShowBudget(false)}
              aria-label={t("Close", "Schließen")}
              className="text-muted-foreground hover:text-foreground"
            >
              <X className="h-3 w-3" />
            </button>
          </div>
          {budget ? (
            <p className="text-muted-foreground">
              {t(
                `${budget.currentTokens.toLocaleString()} tokens used this month` +
                  (budget.hardLimitTokens
                    ? ` of ${budget.hardLimitTokens.toLocaleString()}`
                    : " — no hard limit set"),
                `${budget.currentTokens.toLocaleString()} Tokens diesen Monat verbraucht` +
                  (budget.hardLimitTokens
                    ? ` von ${budget.hardLimitTokens.toLocaleString()}`
                    : " — kein hartes Limit gesetzt"),
              )}
            </p>
          ) : (
            <p className="text-muted-foreground">{t("Loading…", "Wird geladen…")}</p>
          )}
        </div>
      )}

      <div className="border-t border-border p-3">
        {sigil && (
          <SigilPopover
            title={
              sigil.kind === "/"
                ? t("Commands", "Befehle")
                : sigil.kind === "@"
                  ? t("Agents", "Agenten")
                  : t("Attach context", "Kontext anhängen")
            }
            items={sigilItems}
            activeIndex={pickerIndex}
            onPick={pickSigilItem}
            onHoverIndex={setPickerIndex}
            emptyText={
              sigil.kind === "@"
                ? t("No agent matches that.", "Kein Agent passt dazu.")
                : sigil.kind === "/"
                  ? t("No command matches that.", "Kein Befehl passt dazu.")
                  : t("No knowledge base matches that.", "Keine Wissensbasis passt dazu.")
            }
          />
        )}
        {armed && (
          <div className="mb-2 flex items-center gap-1.5 text-[11px]">
            <span className="rounded-full border border-primary/40 bg-primary/10 px-2 py-0.5 font-mono text-primary">
              /{armed.command.key}
            </span>
            <span className="text-muted-foreground">
              {commandSummary(armed.command.key, armed.command.summary, t)}
            </span>
          </div>
        )}
        {contextRefs.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {contextRefs.map((ref) => (
              <span
                key={ref.id}
                className="inline-flex max-w-[200px] items-center gap-1 truncate rounded-full border border-border bg-background/60 py-0.5 pl-2 pr-1 text-[11px] text-muted-foreground"
              >
                <BookOpen className="h-2.5 w-2.5 shrink-0" />
                <span className="truncate">{ref.label}</span>
                <button
                  type="button"
                  onClick={() => setContextRefs((p) => p.filter((r) => r.id !== ref.id))}
                  title={t("Remove", "Entfernen")}
                  className="rounded-full p-0.5 transition hover:text-foreground"
                >
                  <X className="h-2.5 w-2.5" />
                </button>
              </span>
            ))}
          </div>
        )}
        {!isAssistant && (
          <div className="mb-1 text-[11px] text-muted-foreground">
            {t(
              `This conversation goes to ${agents.find((a) => a.id === agentId)?.name ?? "an agent"} — not the copilot.`,
              `Dieses Gespräch geht an ${agents.find((a) => a.id === agentId)?.name ?? "einen Agenten"} — nicht an den Copilot.`,
            )}
          </div>
        )}
        <div className="flex items-end gap-2 rounded-xl border border-border bg-background/40 px-3 py-2 focus-within:border-primary/50">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => {
              setInput(e.target.value);
              setCaret(e.target.selectionStart ?? e.target.value.length);
              setPickerIndex(0);
            }}
            onKeyUp={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
            onClick={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
            onKeyDown={(e) => {
              const open = !!sigil;
              if (open && sigilItems.length > 0) {
                if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                  e.preventDefault();
                  setPickerIndex((i) =>
                    cycleIndex(i, e.key === "ArrowDown" ? 1 : -1, sigilItems.length),
                  );
                  return;
                }
                if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
                  // Enter picks from the picker rather than sending: a reader
                  // mid-`@`/`#`/`/` is choosing, not finished.
                  e.preventDefault();
                  pickSigilItem(sigilItems[pickerIndex]);
                  return;
                }
              }
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            rows={1}
            placeholder={de ? "oc8 konfigurieren oder fragen…" : "Configure or ask oc8…"}
            className="max-h-28 min-h-[24px] flex-1 resize-none bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
          <button
            type="button"
            onClick={send}
            disabled={!input.trim() || busy || !agentId}
            aria-label={de ? "Senden" : "Send"}
            className={cn(
              "grid h-8 w-8 shrink-0 place-items-center rounded-lg transition",
              input.trim() && !busy && agentId
                ? "bg-primary text-primary-foreground hover:brightness-110"
                : "bg-muted/40 text-muted-foreground",
            )}
          >
            <Send className="h-3.5 w-3.5" />
          </button>
        </div>
        <ComposerHint />
      </div>
    </>
  );
}
