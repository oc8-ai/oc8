// frontend/src/components/conversation/conversation.tsx
// The one chat (spec §2.1). Every surface mounts this; only spacing differs
// between "full" (Copilot page, Live tab) and "compact" (dock, My Work).
import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowDown, X } from "lucide-react";

import type { SigilItem } from "@/components/composer-sigils";
import { Skeleton } from "@/components/ui/skeleton";
import { api } from "@/lib/api";
import {
  conversationKeys,
  useRegenerate,
  type ChatSessionDetailDTO,
  type LiveRunDTO,
} from "@/lib/conversation-api";
import { useAuth, useBudgetStatus } from "@/lib/hooks";
import {
  chatKeys,
  useCreateChatSession,
  useSendChatMessage,
  type ChatMessageDTO,
} from "@/lib/hooks-chat";
import { useLang, useT } from "@/lib/i18n";
import { useWatchRuns } from "@/lib/live/provider";
import { cn } from "@/lib/utils";

import { AssistantTurn } from "./assistant-turn";
import { Composer, type ComposerSubmit } from "./composer";
import type { AssistantTurnModel, ConversationVariant, TurnModel } from "./types";
import { useTimelineRuns } from "./timeline-runs";
import { useConversation } from "./use-conversation";
import { useScrollAnchor } from "./use-scroll-anchor";
import { UserTurn } from "./user-turn";

/** A run that is not a chat turn (a schedule, a webhook, a delegation, a
 *  colleague's chat under "All runs"), shown in the conversation at `at`. */
export interface ExtraRunTurn {
  at: string;
  run: LiveRunDTO;
  /** The line above it: "Schedule · 09:00", "Delegated by Copilot", … */
  trigger: ReactNode;
  footer?: ReactNode;
  /** A colleague's run: step labels and result only, no Details. */
  labelsOnly?: boolean;
}

type Row =
  | { at: string; kind: "turn"; turn: TurnModel }
  | { at: string; kind: "run"; extra: ExtraRunTurn };

const TERMINAL = new Set(["done", "failed", "interrupted"]);
/** How close to the top (px) the reader must scroll for older items to load. */
const TOP_THRESHOLD = 120;

/** Each turn's time. An answer still being written has none of its own: it
 *  sits right after the turn before it, as in the transcript. */
function turnRows(turns: TurnModel[]): Row[] {
  let last = "";
  return turns.map((turn): Row => {
    const at =
      turn.kind === "user" ? turn.message.createdAt : (turn.variants[0]?.createdAt ?? last);
    last = at || last;
    return { at, kind: "turn", turn };
  });
}

/** Milliseconds for sorting; a row without a usable time sorts last. */
function timeOf(at: string): number {
  const ms = Date.parse(at);
  return Number.isNaN(ms) ? Number.POSITIVE_INFINITY : ms;
}

function localDay(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? "" : d.toDateString();
}

export interface ConversationProps {
  agentId: string;
  sessionId: string | null;
  onSessionChange: (sessionId: string) => void;
  variant: ConversationVariant;
  /** Shown while the chat has no turns. A function receives `send`, so
   *  prompt starters can go out as the first message (C2 replaces this
   *  with PromptStarters). */
  emptyState?: ReactNode | ((send: (text: string) => void) => ReactNode);
  draft?: string;
  onDraftChange?: (text: string) => void;
  agentItems?: SigilItem[];
  onAddressAgent?: (agentId: string) => void;
  placeholder?: string;
  renderAssistantExtras?: (turn: AssistantTurnModel) => { cards?: ReactNode; footer?: ReactNode };
  composerAbove?: ReactNode;
  composerTarget?: ReactNode;
  /** Beside Send (the Live tab's Stop for a timeline run). */
  composerTrailing?: ReactNode;
  onSendOverride?: (submit: ComposerSubmit) => boolean;
  onStopOverride?: () => void;
  /** Focus the composer on mount (the dock, when it opens). */
  autoFocus?: boolean;
  /** Runs merged with the transcript by time (the Live tab). */
  extraRuns?: ExtraRunTurn[];
  /** A dated line wherever the local day changes. */
  daySeparators?: boolean;
  /** There is more above the oldest item; `onLoadOlder` fetches it when the
   *  reader scrolls to the top. The reader's place is kept as it lands. */
  hasOlder?: boolean;
  onLoadOlder?: () => void;
  loadingOlder?: boolean;
}

function TurnSkeletons() {
  return (
    <div className="space-y-6" aria-busy="true">
      {[0, 1, 2].map((i) => (
        <div key={i} data-testid="turn-skeleton" className="space-y-2">
          <Skeleton className={cn("h-4 rounded-md", i % 2 ? "ml-auto w-1/2" : "w-3/4")} />
          <Skeleton className="h-4 w-2/3 rounded-md" />
        </div>
      ))}
    </div>
  );
}

export function Conversation(props: ConversationProps) {
  const t = useT();
  const qc = useQueryClient();
  const { data: me } = useAuth();
  const { lang } = useLang();
  const conv = useConversation(props.sessionId);
  // A run that is already a chat turn (its answer, or a run still writing
  // one) is shown there, never a second time as a timeline run.
  const turnRunIds = new Set<string>();
  for (const turn of conv.turns) {
    if (turn.kind !== "assistant") continue;
    if (turn.liveRunId) turnRunIds.add(turn.liveRunId);
    for (const v of turn.variants) if (v.runId) turnRunIds.add(v.runId);
  }
  const extraRuns = (props.extraRuns ?? []).filter((e) => !turnRunIds.has(e.run.id));
  // Only my own running runs are watched; a colleague's is refetched with
  // the timeline instead (useLiveTimeline).
  const watchedIds = extraRuns
    .filter((e) => !e.labelsOnly && !TERMINAL.has(e.run.state))
    .map((e) => e.run.id);
  useWatchRuns(watchedIds);
  const timelineRuns = useTimelineRuns(
    extraRuns.map((e) => e.run),
    props.agentId,
    new Set(watchedIds),
  );
  const rows: Row[] = [
    ...turnRows(conv.turns),
    ...extraRuns.map((extra): Row => ({ at: extra.at, kind: "run", extra })),
  ].sort((a, b) => timeOf(a.at) - timeOf(b.at));
  const [ownDraft, setOwnDraft] = useState("");
  const draft = props.draft ?? ownDraft;
  const setDraft = props.onDraftChange ?? setOwnDraft;
  const [sendError, setSendError] = useState<string | null>(null);
  const [showBudget, setShowBudget] = useState(false);
  const { data: budget } = useBudgetStatus(null, { enabled: showBudget });
  const createSession = useCreateChatSession();
  const sendMessage = useSendChatMessage(props.sessionId ?? "");
  const regenerate = useRegenerate(props.sessionId);
  // Any new row re-checks the bottom; an older page landing on top moves
  // only a reader already there (the anchoring below keeps everyone else).
  const newest = rows[rows.length - 1];
  const newestKey = newest ? (newest.kind === "turn" ? newest.turn.key : newest.extra.run.id) : "";
  const { ref, contentRef, atBottom, scrollToBottom } = useScrollAnchor(
    `${newestKey}:${rows.length}:${conv.liveRunIds.join()}:${sendError ?? ""}`,
    props.sessionId,
  );
  // Older items land above the reader: keep what they were looking at where
  // it was, by the growth of the scroll height.
  const oldest = rows[0];
  const oldestKey = oldest ? (oldest.kind === "turn" ? oldest.turn.key : oldest.extra.run.id) : "";
  const beforeLoad = useRef<{ key: string; height: number; top: number } | null>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    const saved = beforeLoad.current;
    if (!el || !saved || saved.key === oldestKey) return;
    beforeLoad.current = null;
    el.scrollTop = saved.top + (el.scrollHeight - saved.height);
  }, [oldestKey, ref]);
  const { hasOlder, onLoadOlder, loadingOlder } = props;
  // A load that ended without new rows on top (failed, or nothing older)
  // must not shift the reader's place on some later change.
  useEffect(() => {
    if (!loadingOlder) beforeLoad.current = null;
  }, [loadingOlder]);
  function loadOlder() {
    const el = ref.current;
    if (!onLoadOlder || !hasOlder || loadingOlder) return;
    if (el) beforeLoad.current = { key: oldestKey, height: el.scrollHeight, top: el.scrollTop };
    onLoadOlder();
  }
  const loadOlderRef = useRef(loadOlder);
  loadOlderRef.current = loadOlder;
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onScroll = () => {
      if (el.scrollTop <= TOP_THRESHOLD) loadOlderRef.current();
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [ref]);
  const readOnly = !!conv.session?.readOnly;
  const busy = conv.liveRunIds.length > 0;
  const lastUser = [...conv.turns].reverse().find((turn) => turn.kind === "user");

  // One creation in flight at a time: a multi-file drop and a send during an
  // upload all wait for the same new chat instead of each making one.
  const creating = useRef<Promise<string> | null>(null);
  useEffect(() => {
    creating.current = null;
  }, [props.sessionId]);

  function ensureSession(): Promise<string> {
    if (props.sessionId) return Promise.resolve(props.sessionId);
    creating.current ??= createSession.mutateAsync(props.agentId).then(
      (session) => {
        // Seed the new chat's caches so it does not flash skeletons between
        // being created and its first message landing.
        qc.setQueryData<ChatMessageDTO[]>(chatKeys.messages(session.id), (prev) => prev ?? []);
        qc.setQueryData<ChatSessionDetailDTO>(
          conversationKeys.session(session.id),
          (prev) =>
            prev ?? {
              ...session,
              modelOverrideId: null,
              kind: "chat",
              activeRuns: [],
              readOnly: false,
            },
        );
        props.onSessionChange(session.id);
        return session.id;
      },
      (error: unknown) => {
        creating.current = null;
        throw error;
      },
    );
    return creating.current;
  }

  /** `fromComposer` false: a prompt starter, which leaves the draft alone. */
  async function onSend(submit: ComposerSubmit, fromComposer = true) {
    if (props.onSendOverride?.(submit)) {
      if (fromComposer) setDraft("");
      return;
    }
    setSendError(null);
    if (fromComposer) setDraft("");
    const body = {
      message: submit.text,
      attachmentIds: submit.attachmentIds,
      contextRefs: submit.contextRefs.map(({ kind, id }) => ({ kind, id })),
    };
    try {
      const sessionId = await ensureSession();
      let message: ChatMessageDTO;
      if (sessionId === props.sessionId) {
        message = await sendMessage.mutateAsync(body);
      } else {
        // `sendMessage` is bound to the id this render had (none yet).
        message = await api.post<ChatMessageDTO>(`/chat/sessions/${sessionId}/messages`, body);
        qc.setQueryData<ChatMessageDTO[]>(chatKeys.messages(sessionId), (prev) => [
          ...(prev ?? []),
          message,
        ]);
      }
      if (message.runId) {
        const run = { runId: message.runId, replyToId: message.id, state: "queued" };
        qc.setQueryData<ChatSessionDetailDTO>(conversationKeys.session(sessionId), (prev) =>
          prev && !prev.activeRuns.some((r) => r.runId === run.runId)
            ? { ...prev, activeRuns: [...prev.activeRuns, run] }
            : prev,
        );
      }
    } catch {
      if (fromComposer) setDraft(submit.text);
      setSendError(
        t(
          "Your message was not sent. Try again.",
          "Deine Nachricht wurde nicht gesendet. Versuch es noch einmal.",
        ),
      );
    }
  }

  async function onStop() {
    if (props.onStopOverride) {
      props.onStopOverride();
      return;
    }
    for (const runId of conv.liveRunIds) {
      await api.post(`/runs/${runId}/stop`).catch(() => undefined);
    }
  }

  function renderTurn(turn: TurnModel) {
    return turn.kind === "user" ? (
      <UserTurn
        turn={turn}
        viewerMemberId={me?.memberId ?? null}
        canEdit={!readOnly && turn === lastUser && !busy}
        onEdit={(text) => setDraft(text)}
      />
    ) : (
      <AssistantTurn
        sessionId={props.sessionId}
        turn={turn}
        readOnly={readOnly}
        onRegenerate={(replyToId) => regenerate.mutate(replyToId)}
        {...(props.renderAssistantExtras?.(turn) ?? {})}
      />
    );
  }

  const sendText = (text: string) => {
    if (busy || readOnly) return;
    void onSend({ text, attachmentIds: [], contextRefs: [] }, false);
  };
  const empty =
    typeof props.emptyState === "function" ? props.emptyState(sendText) : props.emptyState;
  const full = props.variant === "full";
  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      <div
        ref={ref}
        data-testid="conversation-scroller"
        className={cn("min-h-0 flex-1 overflow-y-auto", full ? "px-4 py-6 md:px-6" : "px-3 py-4")}
      >
        <div
          ref={contentRef}
          className={cn("mx-auto w-full max-w-[760px]", full ? "space-y-6" : "space-y-4")}
        >
          {hasOlder && onLoadOlder && !conv.isLoading && (
            <div className="flex justify-center">
              {loadingOlder ? (
                <Skeleton className="h-4 w-1/3 rounded-md" aria-label={t("Loading", "Lädt")} />
              ) : (
                <button
                  type="button"
                  onClick={loadOlder}
                  className="inline-flex min-h-9 items-center rounded-md px-2 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {t("Show earlier", "Früheres anzeigen")}
                </button>
              )}
            </div>
          )}
          {conv.isLoading ? (
            <TurnSkeletons />
          ) : rows.length === 0 ? (
            (empty ?? null)
          ) : (
            rows.map((row, i) => (
              <Fragment key={row.kind === "turn" ? row.turn.key : `extra-${row.extra.run.id}`}>
                {props.daySeparators && localDay(row.at) !== localDay(rows[i - 1]?.at ?? "") && (
                  <div
                    role="separator"
                    className="flex items-center gap-3 text-micro text-muted-foreground"
                  >
                    <span className="h-px flex-1 bg-border" />
                    {new Date(row.at).toLocaleDateString(lang === "de" ? "de-DE" : "en-US", {
                      weekday: "short",
                      day: "numeric",
                      month: "short",
                    })}
                    <span className="h-px flex-1 bg-border" />
                  </div>
                )}
                {row.kind === "run" ? (
                  <AssistantTurn
                    sessionId={null}
                    turn={{
                      kind: "assistant",
                      key: row.extra.run.id,
                      replyToId: null,
                      variants: [],
                      liveRunId: row.extra.run.id,
                    }}
                    run={timelineRuns[row.extra.run.id]}
                    trigger={row.extra.trigger}
                    footer={row.extra.footer}
                    labelsOnly={row.extra.labelsOnly}
                    readOnly
                  />
                ) : (
                  renderTurn(row.turn)
                )}
              </Fragment>
            ))
          )}

          {sendError && (
            <p role="alert" className="text-xs text-status-error">
              {sendError}
            </p>
          )}
        </div>
      </div>
      <div
        className={cn(
          "relative mx-auto w-full max-w-[760px]",
          full ? "px-4 pb-4 md:px-6" : "px-3 pb-3",
        )}
      >
        {!atBottom && (
          <button
            type="button"
            onClick={scrollToBottom}
            className="absolute -top-10 left-1/2 inline-flex -translate-x-1/2 items-center gap-1 rounded-full border border-border bg-popover px-3 py-1 text-xs text-foreground shadow-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <ArrowDown className="size-3.5" aria-hidden />
            {t("Latest", "Neueste")}
          </button>
        )}
        {showBudget && (
          <div
            className="mb-2 rounded-lg border border-border bg-card px-3 py-2 text-xs"
            aria-live="polite"
          >
            <div className="mb-1 flex items-center justify-between">
              <span className="font-medium">{t("Budget", "Budget")}</span>
              <button
                type="button"
                onClick={() => setShowBudget(false)}
                aria-label={t("Close", "Schließen")}
                className="rounded-md text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <X className="size-3" aria-hidden />
              </button>
            </div>
            {budget ? (
              <p className="text-muted-foreground">
                {t(
                  `${budget.currentTokens.toLocaleString("en-US")} tokens used this month` +
                    (budget.hardLimitTokens
                      ? ` of ${budget.hardLimitTokens.toLocaleString("en-US")}`
                      : " — no hard limit set"),
                  `${budget.currentTokens.toLocaleString("de-DE")} Tokens diesen Monat verbraucht` +
                    (budget.hardLimitTokens
                      ? ` von ${budget.hardLimitTokens.toLocaleString("de-DE")}`
                      : " — kein hartes Limit gesetzt"),
                )}
              </p>
            ) : (
              <Skeleton className="h-3 w-1/2 rounded-md" />
            )}
          </div>
        )}
        <Composer
          sessionId={props.sessionId}
          ensureSession={ensureSession}
          draft={draft}
          onDraftChange={setDraft}
          busy={busy}
          onSend={(s) => void onSend(s)}
          onStop={() => void onStop()}
          onEditLast={
            lastUser && lastUser.kind === "user" && !busy && !readOnly
              ? () => setDraft(lastUser.message.content)
              : undefined
          }
          onLocalCommand={(key) => {
            if (key === "budget") setShowBudget(true);
          }}
          placeholder={props.placeholder}
          modelValue={conv.session?.modelOverrideId ?? null}
          showModelPicker={!!props.sessionId}
          agentItems={props.agentItems}
          onAddressAgent={props.onAddressAgent}
          target={props.composerTarget}
          trailing={props.composerTrailing}
          above={props.composerAbove}
          disabled={readOnly}
          autoFocus={props.autoFocus}
        />
      </div>
    </div>
  );
}
