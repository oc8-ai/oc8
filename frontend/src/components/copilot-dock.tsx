import { BookOpen, ChevronDown, Plus, Send, Sparkles, WifiOff, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { ChatMarkdown } from "@/components/chat-markdown";
import { CopilotRunActivity, CopilotStreamingAnswer } from "@/components/copilot-run-activity";
import { RUN_COMPONENT_REGISTRY } from "@/components/run-record-card";
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
import { useCan, useMay } from "@/lib/governance-hooks";
import {
  chatCommands,
  commandSummary,
  detectCommand,
  matchCommands,
  useChatModes,
} from "@/lib/chat-commands";
import type { Agent, Department } from "@/lib/mock-data";
import type { CopilotProposal } from "@/lib/hooks";
import {
  useAgents,
  useApplyCopilotProposal,
  useApprovals,
  useAssistant,
  useAuth,
  useBudgetStatus,
  useCopilotProposals,
  useDepartments,
  useKnowledgeBases,
  useRejectCopilotProposal,
} from "@/lib/hooks";
import { nextStepChips } from "@/lib/chat-suggestions";
import {
  useChatSessions,
  useCreateChatSession,
  useChatMessages,
  useCopilotRunActivity,
  useSendChatMessage,
} from "@/lib/hooks-chat";
import { useLiveConnectionStatus } from "@/lib/live/provider";
import { ChatSessionPicker } from "@/components/chat-window";

// Set by DoneStep right before the full-page navigation into "/" that follows
// onboarding — sessionStorage (not React state) because that navigation
// remounts the whole app, so nothing in memory survives it.
export const COPILOT_AUTO_OPEN_KEY = "oc8-copilot-auto-open";

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

function proposalFailedText(de: boolean): string {
  return de
    ? "Der Vorschlag konnte nicht bearbeitet werden — vielleicht hat ihn jemand anderes schon beantwortet. Lade die Seite neu oder versuch es noch einmal."
    : "That proposal could not be answered — somebody else may have answered it already. Reload or try again.";
}

// The tab bar's fallback label for a tab whose session doesn't exist yet
// (a brand new blank tab) or whose session exists but has no title yet (the
// same "no title" state ChatSessionPicker itself falls back on, worded the
// same way, deliberately NOT "New chat" -- that copy is reserved for the
// header's own "reset to a blank tab" button, and re-using it here would
// make a tab pill and that button indistinguishable by accessible name).
function untitledChatText(de: boolean): string {
  return de ? "Unbenannter Chat" : "Untitled chat";
}

// A proposal the Assistant drafted, with the two buttons that answer it.
//
// The Assistant's `propose_change` tool may only ever DRAFT (control_tools.py:
// "apply_proposal is never reachable from here"), so a structural change does
// nothing at all until somebody presses Apply here. Between the Task 6 dock
// rewrite and this, there was no live surface that could: the mutations
// existed and had zero call sites, so every proposal the Assistant made --
// over the web or over Telegram -- simply sat in the database.
export function PendingProposals({ de }: { de: boolean }) {
  const can = useCan();
  const { data: proposals } = useCopilotProposals({ poll: true, enabled: can("copilot:view") });
  const apply = useApplyCopilotProposal();
  const reject = useRejectCopilotProposal();
  // Answered here-and-now, on top of what the server last said: the list is
  // refetched after each mutation, but until that lands the card the reader
  // just answered must not sit there looking unanswered.
  const [answered, setAnswered] = useState<string[]>([]);
  // The optimistic mark above is a GUESS, and a wrong guess here is the worst
  // kind: apply/reject can be refused (409 when somebody else answered the
  // proposal first, or any transport failure), and without this the card
  // simply vanished while the proposal stayed `draft` — the reader is told
  // nothing and believes a structural change went through that did not.
  const [failed, setFailed] = useState(false);

  // Same shape the chat send below uses (`sendMessage.mutate({ message }, {
  // onError: ... })`): mark optimistically, undo the mark and show a notice
  // if the server refuses.
  function answer(id: string, run: (options: { onError: () => void }) => void) {
    setFailed(false);
    setAnswered((ids) => [...ids, id]);
    run({
      onError: () => {
        setAnswered((ids) => ids.filter((answeredId) => answeredId !== id));
        setFailed(true);
      },
    });
  }

  const pending = (proposals ?? []).filter(
    (p: CopilotProposal) => p.status === "draft" && !answered.includes(p.id),
  );
  // The notice outlives the card on purpose: a 409 means the proposal really
  // is no longer `draft`, so the refetched list drops it — and that is exactly
  // the case where a silently disappearing card would be read as success.
  if (pending.length === 0 && !failed) return null;

  return (
    <section
      aria-label={de ? "Offene Vorschläge" : "Pending proposals"}
      className="border-b border-border bg-primary/5 px-4 py-3"
    >
      <h3 className="mb-2 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        <Sparkles className="h-3 w-3 text-primary" />
        {de ? "Offene Vorschläge" : "Pending proposals"}
      </h3>
      {failed && (
        <p
          role="alert"
          className="mb-2 rounded-md border border-destructive/40 bg-destructive/10 px-2.5 py-1.5 text-[11px] text-destructive"
        >
          {proposalFailedText(de)}
        </p>
      )}
      <ul className="space-y-2">
        {pending.map((proposal: CopilotProposal) => (
          <li
            key={proposal.id}
            className="rounded-lg border border-border bg-background/50 p-2.5 text-xs"
          >
            <ul className="space-y-1">
              {proposal.operations.map((operation, index) => (
                <li key={`${operation.label}-${index}`}>
                  <span className="font-mono text-foreground">{operation.label}</span>
                  {Object.entries(operation.references).map(([label, reference]) => (
                    <span key={label} className="ml-2 text-muted-foreground">
                      {label}: <code>{reference}</code>
                    </span>
                  ))}
                </li>
              ))}
            </ul>
            {can("copilot:manage") && (
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  disabled={apply.isPending || reject.isPending}
                  onClick={() =>
                    answer(proposal.id, (options) => apply.mutate(proposal.id, options))
                  }
                  className="rounded-md bg-primary px-2.5 py-1 text-[11px] font-medium text-primary-foreground disabled:opacity-50"
                >
                  {de ? "Anwenden" : "Apply"}
                </button>
                <button
                  type="button"
                  disabled={apply.isPending || reject.isPending}
                  onClick={() =>
                    answer(proposal.id, (options) => reject.mutate(proposal.id, options))
                  }
                  className="rounded-md border border-border px-2.5 py-1 text-[11px] disabled:opacity-50"
                >
                  {de ? "Ablehnen" : "Reject"}
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

// One open conversation in the dock's tab bar. `sessionId` is `null` for a
// tab that hasn't lazily created its session yet (a brand new tab, same
// "blank until you send" state the single-session dock always started in).
export interface CopilotTab {
  uiId: string;
  sessionId: string | null;
  /** Which agent this tab is talking to. Absent = the tenant assistant, which
   *  is every tab until somebody types `@`. Persisted with the tab, so a
   *  reopened dock reopens the same conversation with the same agent. */
  agentId?: string;
}

function tabsStorageKey(memberId: string): string {
  return `oc8-copilot-tabs-${memberId}`;
}

function loadTabs(memberId: string): CopilotTab[] {
  try {
    const raw = localStorage.getItem(tabsStorageKey(memberId));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (t): t is CopilotTab =>
        typeof t === "object" && t !== null && typeof (t as CopilotTab).uiId === "string",
    );
  } catch {
    return [];
  }
}

// `CopilotDock` is mounted unconditionally, once, for the whole app session
// (app-shell.tsx) -- so the permission check has to happen here, BEFORE
// `CopilotDockPanel` (and its chat-pipeline query hooks) ever mounts, not
// inside it. A check at the bottom of a component that has already called
// `useAssistant()`/`useChatSessions()` earlier in its body is too late: those
// are plain `useQuery` calls with no `enabled` gate, so they fire on mount
// regardless of what a later `if (!mayUseCopilot) return null` decides --
// every signed-in user would otherwise cause a GET /assistant + GET
// /chat/sessions (a 403 for anyone without copilot:use) on every page
// load, permitted or not.
export function CopilotDock() {
  const can = useCan();
  // Every human role holds copilot:use by default (see COPILOT_USE's own
  // docstring) -- this is the door that opens the Copilot to everyone.
  if (!can("copilot:use")) return null;
  return <CopilotDockPanel />;
}

// The floating oc8 Copilot dock -- a direct 1:1 chat with the tenant's
// standing Assistant agent (GET /assistant), rendered as a compact overlay
// instead of a full page like ChatWindow/chat-window.tsx. Reuses the exact
// same session/message hooks and session-bootstrap logic as ChatWindow (see
// its own doc comment) rather than a separate mechanism.
//
// This panel owns the tab bar: which sessions are open (`tabs`, persisted to
// localStorage per member), which one is active, and the header/proposals
// that are shared across every tab. Each tab's own single-session lifecycle
// (composer draft, send-in-flight state, the lazy-create-on-send flow) lives
// in `CopilotChatTab` below, unchanged from what this file used to do with
// one session at a time.
function CopilotDockPanel() {
  const t = useT();
  const de = t("en", "de") === "de";
  const [open, setOpen] = useState(false);

  const { data: assistant } = useAssistant();
  const assistantAgentId = assistant?.agentId;
  const { data: sessions } = useChatSessions(assistantAgentId);
  // Titles for EVERY session this member has, not just the assistant's: a tab
  // may now address another agent, and its title comes from the same list.
  // `useChatSessions(assistantAgentId)` above stays as-is -- ChatSessionPicker
  // is the assistant's own history and must not start listing other agents'.
  const { data: allSessions } = useChatSessions();
  const may = useMay();
  const { data: agentPage } = useAgents({ pageSize: 100 });
  const { data: departmentPage } = useDepartments({ pageSize: 100 });
  const liveConnectionStatus = useLiveConnectionStatus();

  const { data: me, isError: authFailed } = useAuth();
  const memberId = me !== undefined ? (me.memberId ?? "anon") : authFailed ? "anon" : undefined;

  const [tabs, setTabs] = useState<CopilotTab[]>([]);
  const [activeUiId, setActiveUiId] = useState<string | null>(null);
  // Tracks which member's tabs are currently loaded into `tabs` above, so the
  // persistence and bootstrap effects below never fire before the matching
  // load has actually committed (they would otherwise write/derive from the
  // OLD member's -- or the not-yet-resolved "anon" placeholder's -- tabs,
  // destroying whatever was saved for the new member). `undefined` means "not
  // loaded for anyone yet."
  //
  // This is STATE, not a ref: a ref would flip synchronously the moment the
  // load effect below runs, making it already look "loaded" to the
  // persist/bootstrap effects that fire in that very same effect flush --
  // while `tabs` itself is still the pre-load value from this same render
  // (React batches all three effects' setState calls from one flush into a
  // single next render, so their closures over `tabs` don't see each other's
  // updates). That reintroduces the exact clobbering race this fix exists to
  // close, just one level down, whenever `sessions` has already resolved by
  // the time `memberId` first does. Using state instead defers visibility to
  // the NEXT render, by which point `tabs` genuinely reflects the load.
  const [loadedMemberId, setLoadedMemberId] = useState<string | undefined>(undefined);

  // Composer draft text, keyed by uiId, lifted out of `CopilotChatTab` so it
  // survives that tab's instance unmounting and remounting when the dock is
  // closed and reopened (every `CopilotChatTab` lives inside `{open && ...}`
  // below, so closing the dock unmounts them all).
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  function setDraft(uiId: string, text: string) {
    setDrafts((prev) => (prev[uiId] === text ? prev : { ...prev, [uiId]: text }));
  }

  useEffect(() => {
    if (memberId === undefined) return; // /me hasn't resolved yet
    if (loadedMemberId === memberId) return; // already loaded for this member
    const loaded = loadTabs(memberId);
    setTabs(loaded);
    setActiveUiId(loaded[0]?.uiId ?? null);
    setLoadedMemberId(memberId);
  }, [memberId, loadedMemberId]);

  useEffect(() => {
    if (loadedMemberId === undefined || loadedMemberId !== memberId) return; // don't persist before the load above has committed
    localStorage.setItem(tabsStorageKey(loadedMemberId), JSON.stringify(tabs));
  }, [tabs, memberId, loadedMemberId]);

  // Today's own bootstrap-to-most-recent-session behaviour, now scoped to
  // "no tabs at all yet" instead of "no session yet" -- a returning user with
  // persisted tabs skips this and reopens exactly where they left off. Gated
  // on `sessions` having actually resolved (not merely falsy, which is also
  // the loading state) so a returning-with-zero-tabs caller doesn't get
  // locked into a blank tab a moment before the real most-recent session
  // lands: when it resolves empty, a single blank tab is exactly today's own
  // starting state (a composer ready for lazy session creation), so this
  // always leaves at least one tab open, matching the old single-session dock
  // never having a "nothing to show" state.
  useEffect(() => {
    if (loadedMemberId === undefined || loadedMemberId !== memberId) return; // wait for the per-member load above to commit
    if (tabs.length > 0) return;
    if (!sessions) return;
    const uiId = crypto.randomUUID();
    setTabs([{ uiId, sessionId: sessions[0]?.id ?? null }]);
    setActiveUiId(uiId);
  }, [memberId, loadedMemberId, tabs.length, sessions]);

  function openNewTab() {
    const uiId = crypto.randomUUID();
    setTabs((prev) => [...prev, { uiId, sessionId: null }]);
    setActiveUiId(uiId);
  }

  function focusOrOpenSessionTab(sessionId: string) {
    const existing = tabs.find((tab) => tab.sessionId === sessionId);
    if (existing) {
      setActiveUiId(existing.uiId);
      return;
    }
    const uiId = crypto.randomUUID();
    setTabs((prev) => [...prev, { uiId, sessionId }]);
    setActiveUiId(uiId);
  }

  function closeTab(uiId: string) {
    setTabs((prev) => {
      const next = prev.filter((tab) => tab.uiId !== uiId);
      if (activeUiId === uiId) {
        setActiveUiId(next[next.length - 1]?.uiId ?? null);
      }
      return next;
    });
    setDrafts((prev) => {
      if (!(uiId in prev)) return prev;
      const { [uiId]: _removed, ...rest } = prev;
      return rest;
    });
  }

  function setTabSession(uiId: string, sessionId: string | null) {
    setTabs((prev) => prev.map((tab) => (tab.uiId === uiId ? { ...tab, sessionId } : tab)));
  }

  function setTabAgent(uiId: string, agentId: string | undefined) {
    // A new addressee is a new conversation: `ChatSession.agent_id` and its
    // Task are a pair (oc8/chat/service.py), so the session is reset rather
    // than re-pointed. Phase 2 replaces this with a real participant list.
    setTabs((prev) =>
      prev.map((tab) => (tab.uiId === uiId ? { ...tab, agentId, sessionId: null } : tab)),
    );
  }

  useEffect(() => {
    if (window.sessionStorage.getItem(COPILOT_AUTO_OPEN_KEY) === "1") {
      window.sessionStorage.removeItem(COPILOT_AUTO_OPEN_KEY);
      setOpen(true);
    }
  }, []);

  const activeTab = tabs.find((tab) => tab.uiId === activeUiId);

  return (
    <>
      {open && (
        <div className="fixed bottom-24 right-5 z-50 flex h-[min(72vh,600px)] w-[min(94vw,400px)] flex-col overflow-hidden rounded-2xl border border-border bg-panel shadow-[0_30px_80px_-30px_oklch(0_0_0/80%)]">
          <header className="flex items-center gap-2.5 border-b border-border px-4 py-3">
            <img
              src="/octopus_oc8.svg"
              alt=""
              className="h-8 w-8 shrink-0 select-none"
              draggable={false}
            />
            <div className="min-w-0 flex-1 leading-tight">
              <div className="font-serif text-base lowercase">oc8 copilot</div>
              <div className="inline-flex items-center gap-1.5 text-[11px] text-muted-foreground">
                <span className="h-1.5 w-1.5 rounded-full bg-[color:var(--status-running)] shadow-[0_0_8px_var(--status-running)]" />
                {de ? "bereit" : "ready"}
              </div>
            </div>
            {assistantAgentId && (
              <button
                type="button"
                onClick={() => {
                  // Resets the ACTIVE tab back to a blank, not-yet-created
                  // session -- same affordance as the old single-session
                  // dock's "New chat" button, just aimed at whichever tab is
                  // currently focused instead of the dock's only session.
                  if (activeUiId) setTabSession(activeUiId, null);
                }}
                aria-label={de ? "Neuer Chat" : "New chat"}
                title={de ? "Neuer Chat" : "New chat"}
                className="grid h-8 w-8 shrink-0 place-items-center rounded-md text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
              >
                <Plus className="h-4 w-4" />
              </button>
            )}
            <button
              type="button"
              onClick={() => setOpen(false)}
              aria-label={de ? "Copilot schließen" : "Close copilot"}
              className="grid h-8 w-8 place-items-center rounded-md text-muted-foreground transition hover:bg-muted/40 hover:text-foreground"
            >
              <ChevronDown className="h-4 w-4" />
            </button>
          </header>

          <div className="flex items-center gap-1 overflow-x-auto border-b border-border px-2 py-1">
            {tabs.map((tab) => {
              const session = allSessions?.find((s) => s.id === tab.sessionId);
              const agentName = tab.agentId
                ? (agentPage?.items.find((a) => a.id === tab.agentId)?.name ?? untitledChatText(de))
                : null;
              const title = session?.title || agentName || untitledChatText(de);
              return (
                <div
                  key={tab.uiId}
                  data-copilot-tab={tab.uiId}
                  className={cn(
                    "flex shrink-0 items-center gap-0.5 rounded-md px-1 text-xs",
                    tab.uiId === activeUiId ? "bg-muted" : "hover:bg-muted/50",
                  )}
                >
                  <button
                    type="button"
                    onClick={() => setActiveUiId(tab.uiId)}
                    aria-label={de ? `${title} – Tab` : `${title} tab`}
                    className="max-w-[120px] overflow-hidden text-ellipsis whitespace-nowrap px-1 py-1"
                  >
                    {title}
                  </button>
                  <button
                    type="button"
                    onClick={() => closeTab(tab.uiId)}
                    aria-label={de ? `${title} schließen` : `Close ${title}`}
                    className="grid h-4 w-4 shrink-0 place-items-center opacity-60 hover:opacity-100"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </div>
              );
            })}
            <button
              type="button"
              onClick={openNewTab}
              aria-label={t("New tab", "Neuer Tab")}
              className="grid h-6 w-6 shrink-0 place-items-center rounded-md hover:bg-muted/50"
            >
              <Plus className="h-4 w-4" />
            </button>
            {assistantAgentId && sessions && sessions.length > 0 && (
              <ChatSessionPicker
                agentId={assistantAgentId}
                sessions={sessions}
                sessionId={activeTab?.sessionId ?? null}
                onSelect={(sid) => {
                  if (sid === null) {
                    // ChatSessionPicker calls onSelect(null) after deleting
                    // the ACTIVE session -- reset that tab to blank, same as
                    // the header's own "New chat" button, rather than opening
                    // a new tab.
                    if (activeUiId) setTabSession(activeUiId, null);
                    return;
                  }
                  focusOrOpenSessionTab(sid);
                }}
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

          {tabs.map((tab) => (
            <CopilotChatTab
              key={tab.uiId}
              active={tab.uiId === activeUiId}
              sessionId={tab.sessionId}
              onSessionChange={(sid) => setTabSession(tab.uiId, sid)}
              agentId={tab.agentId ?? assistantAgentId}
              isAssistant={!tab.agentId}
              agents={agentPage?.items ?? []}
              departments={departmentPage?.items ?? []}
              mayStartRuns={may("run:start")}
              onAddressAgent={(agentId) =>
                setTabAgent(tab.uiId, agentId === assistantAgentId ? undefined : agentId)
              }
              draft={drafts[tab.uiId] ?? ""}
              onDraftChange={(text) => setDraft(tab.uiId, text)}
            />
          ))}
          {tabs.length === 0 && (
            <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-muted-foreground">
              {greeting(de)}
            </div>
          )}
        </div>
      )}

      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label={de ? "oc8 Copilot" : "oc8 copilot"}
        className="group fixed bottom-5 right-5 z-50 grid h-14 w-14 place-items-center rounded-full border border-primary/30 bg-panel shadow-[0_16px_40px_-16px_oklch(0_0_0/90%)] transition hover:scale-105"
      >
        {open ? (
          <X className="h-5 w-5 text-muted-foreground" />
        ) : (
          <>
            <img src="/octopus_oc8.svg" alt="" className="h-9 w-9 select-none" draggable={false} />
            <span className="absolute -right-0.5 -top-0.5 grid h-4 w-4 place-items-center rounded-full bg-primary text-primary-foreground">
              <Sparkles className="h-2.5 w-2.5" />
            </span>
          </>
        )}
      </button>
    </>
  );
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
              {m.renderedComponents.length > 0 && (
                <div className="ml-8 space-y-2">
                  {m.renderedComponents.map((c, i) => {
                    const Renderer = Object.prototype.hasOwnProperty.call(
                      RUN_COMPONENT_REGISTRY,
                      c.componentKey,
                    )
                      ? RUN_COMPONENT_REGISTRY[c.componentKey]
                      : undefined;
                    return Renderer ? <Renderer key={i} props={c.props} /> : null;
                  })}
                </div>
              )}
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
