import { ChevronDown, Plus, Sparkles, WifiOff, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "@tanstack/react-router";
import { cn } from "@/lib/utils";
import { urlId } from "@/lib/ids";
import { CopilotPersonaHeader } from "@/components/copilot-persona";
import type { SigilItem } from "@/components/composer-sigils";
import { Conversation } from "@/components/conversation";
import { copilotStarters } from "@/components/conversation/copilot-starters";
import { useT } from "@/lib/i18n";
import { useCan, useMay } from "@/lib/governance-hooks";
import type { CopilotProposal } from "@/lib/hooks";
import {
  useAgents,
  useApplyCopilotProposal,
  useAssistant,
  useAuth,
  useCopilotProposals,
  useDepartments,
  useRejectCopilotProposal,
} from "@/lib/hooks";
import { mostRecentSession } from "@/lib/chat-sessions";
import { useChatSessions } from "@/lib/hooks-chat";
import { useLiveConnectionStatus } from "@/lib/live/provider";
import { SessionPicker } from "@/components/conversation/session-picker";

// Set by DoneStep right before the full-page navigation into "/" that follows
// onboarding — sessionStorage (not React state) because that navigation
// remounts the whole app, so nothing in memory survives it.
export const COPILOT_AUTO_OPEN_KEY = "oc8-copilot-auto-open";

function greeting(de: boolean): string {
  return de
    ? "Hi, ich bin der oc8 Copilot. Ich kenne deine Agenten, Abteilungen und Guardrails — frag mich alles oder lass mich etwas konfigurieren."
    : "Hi, I'm the oc8 copilot. I know your agents, departments and guardrails — ask me anything or let me configure something.";
}

function proposalFailedText(de: boolean): string {
  return de
    ? "Der Vorschlag konnte nicht bearbeitet werden — vielleicht hat ihn jemand anderes schon beantwortet. Lade die Seite neu oder versuch es noch einmal."
    : "That proposal could not be answered — somebody else may have answered it already. Reload or try again.";
}

// The tab bar's fallback label for a tab whose session doesn't exist yet
// (a brand new blank tab) or whose session exists but has no title yet (the
// same "no title" state SessionPicker itself falls back on, worded the
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
    // Stored ids go into API paths: a tab whose ids are not UUIDs keeps its
    // place but starts a new chat with the assistant.
    return parsed
      .filter(
        (t): t is CopilotTab =>
          typeof t === "object" && t !== null && typeof (t as CopilotTab).uiId === "string",
      )
      .map((t) => ({
        uiId: t.uiId,
        sessionId: urlId(t.sessionId) ?? null,
        agentId: urlId(t.agentId),
      }));
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
// instead of the full Copilot page. Both mount the same `Conversation`.
//
// This panel owns the tab bar: which sessions are open (`tabs`, persisted to
// localStorage per member), which one is active, and the header/proposals
// that are shared across every tab. The active tab's chat itself is the one
// `Conversation` every chat surface mounts (spec §2.1).
function CopilotDockPanel() {
  const t = useT();
  const de = t("en", "de") === "de";
  const [open, setOpen] = useState(false);

  const { data: assistant } = useAssistant();
  const assistantAgentId = assistant?.agentId;
  const { data: sessions } = useChatSessions(assistantAgentId);
  // Titles for EVERY session this member has, not just the assistant's: a tab
  // may now address another agent, and its title comes from the same list.
  // `useChatSessions(assistantAgentId)` above stays as-is -- SessionPicker
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

  // Composer draft text, keyed by uiId, kept here so it survives the active
  // tab's chat unmounting on a tab switch or when the dock is closed and
  // reopened (the chat lives inside `{open && ...}` below).
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
    setTabs([{ uiId, sessionId: mostRecentSession(sessions)?.id ?? null }]);
    setActiveUiId(uiId);
  }, [memberId, loadedMemberId, tabs.length, sessions]);

  // Tabs persisted while the member had no chat yet are all blank. Once per
  // load, the active one opens the most recent chat instead, so the dock
  // never greets a member who has chats with an empty one. A tab blanked
  // later (New chat, New tab) stays blank.
  const seededFor = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (loadedMemberId === undefined || loadedMemberId !== memberId) return;
    if (seededFor.current === loadedMemberId || !sessions) return;
    seededFor.current = loadedMemberId;
    const recent = mostRecentSession(sessions);
    if (!recent || tabs.length === 0 || tabs.some((tab) => tab.sessionId)) return;
    const target = activeUiId ?? tabs[0].uiId;
    setTabs((prev) =>
      prev.map((tab) => (tab.uiId === target ? { ...tab, sessionId: recent.id } : tab)),
    );
  }, [memberId, loadedMemberId, sessions, tabs, activeUiId]);

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
  const activeAgentId = activeTab?.agentId ?? assistantAgentId;
  const agents = agentPage?.items ?? [];
  const activeAgentName = activeTab?.agentId
    ? agents.find((a) => a.id === activeTab.agentId)?.name
    : undefined;
  const mayStartRuns = may("run:start");
  // The `@` list -- this is the front door, so switching who you are talking
  // to belongs here (§5.4). The composer filters it by what is typed.
  const agentItems: SigilItem[] = agents
    .filter((a) => !a.deletedAt)
    .map((a) => ({
      id: a.id,
      label: a.name,
      hint:
        [a.role, departmentPage?.items.find((d) => d.id === a.departmentId)?.name]
          .filter(Boolean)
          .join(" · ") || undefined,
      // Shown but not choosable, with the reason: POST
      // /chat/sessions/{id}/messages needs run:start for any agent that is not
      // the assistant, so offering it silently would end in a 403.
      blocked:
        !mayStartRuns && a.id !== activeAgentId
          ? t(
              "You may not start runs for another agent.",
              "Du darfst für andere Agenten keine Läufe starten.",
            )
          : undefined,
    }));

  return (
    <>
      {open && (
        <div className="fixed bottom-24 right-5 z-50 flex h-[min(72vh,600px)] w-[min(94vw,400px)] flex-col overflow-hidden rounded-2xl border border-border bg-panel shadow-[0_30px_80px_-30px_oklch(0_0_0/80%)]">
          <header className="flex items-center gap-2.5 border-b border-border px-4 py-3">
            <CopilotPersonaHeader variant="dock">
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
              <Link to="/copilot" className="text-xs underline-offset-2 hover:underline">
                {de ? "Copilot öffnen" : "Open Copilot"}
              </Link>
            </CopilotPersonaHeader>
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
              <SessionPicker
                sessions={sessions}
                sessionId={activeTab?.sessionId ?? null}
                onSelect={(sid) => {
                  if (sid === null) {
                    // SessionPicker calls onSelect(null) after deleting
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

          {activeTab && activeAgentId && (
            <Conversation
              key={activeTab.uiId}
              agentId={activeAgentId}
              sessionId={activeTab.sessionId}
              onSessionChange={(sid) => setTabSession(activeTab.uiId, sid)}
              variant="compact"
              // A tab addressed to another agent is that agent's chat.
              agentMode={!activeTab.agentId}
              autoFocus
              draft={drafts[activeTab.uiId] ?? ""}
              onDraftChange={(text) => setDraft(activeTab.uiId, text)}
              agentItems={agentItems}
              onAddressAgent={(agentId) =>
                setTabAgent(activeTab.uiId, agentId === assistantAgentId ? undefined : agentId)
              }
              placeholder={t("Configure or ask oc8…", "oc8 konfigurieren oder fragen …")}
              emptyState={<p className="py-6 text-sm text-muted-foreground">{greeting(de)}</p>}
              promptStarters={activeTab.agentId ? undefined : copilotStarters(t)}
              composerAbove={
                activeTab.agentId ? (
                  <p className="text-xs text-muted-foreground">
                    {t(
                      `This chat goes to ${activeAgentName ?? "an agent"} — not the copilot.`,
                      `Dieser Chat geht an ${activeAgentName ?? "einen Agenten"} — nicht an den Copilot.`,
                    )}
                  </p>
                ) : undefined
              }
            />
          )}
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
