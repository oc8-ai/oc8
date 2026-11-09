// frontend/src/components/conversation/conversation.tsx
// The one chat (spec §2.1). Every surface mounts this; only spacing differs
// between "full" (Copilot page, Live tab) and "compact" (dock, My Work).
import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowDown, BookOpen, Bot, Database, X } from "lucide-react";

import type { SigilItem } from "@/components/composer-sigils";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import {
  conversationKeys,
  MAX_PINS,
  refusedRefs,
  useAnswerQuestion,
  useRegenerate,
  useSetPins,
  type ChatSessionDetailDTO,
  type ContextItem,
  type LiveRunDTO,
} from "@/lib/conversation-api";
import { nextStepChips } from "@/lib/chat-suggestions";
import { mayActOn, useApprovals, useAuth, useBudgetStatus, type Approval } from "@/lib/hooks";
import {
  chatKeys,
  useCreateChatSession,
  useSendChatMessage,
  type ChatMessageDTO,
} from "@/lib/hooks-chat";
import { useMay } from "@/lib/governance-hooks";
import { useLang, useT } from "@/lib/i18n";
import { useWatchRuns } from "@/lib/live/provider";
import { cn } from "@/lib/utils";

import { AssistantTurn } from "./assistant-turn";
import { Composer, type ComposerSubmit, type PlusMenuItem } from "./composer";
import { ContextSearchDialog } from "./context-search";
import { DataSourceBrowser } from "./data-source-browser";
import { DecisionCard } from "./decision-card";
import { PinnedBar } from "./pinned-bar";
import { PromptStarters } from "./prompt-starters";
import { QuestionCard } from "./question-card";
import { SuggestionChips, type ReplyChip } from "./suggestion-chips";
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
/** Per-message refs, as the composer's own limit. */
const MAX_MESSAGE_REFS = 10;
/** What "oc8 object" in the + menu searches. */
const OBJECT_KINDS = ["agent", "department", "run", "approval", "responsibility", "chat_session"];
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
   *  anything in it can go out as the first message. */
  emptyState?: ReactNode | ((send: (text: string) => void) => ReactNode);
  /** Cards under the empty state; picking one sends its title. */
  promptStarters?: { title: string; subtitle: string }[];
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
  // The question the chat's run is parked on (spec §4.1). That run waits for
  // the reader, so it does not make the chat busy: the composer answers it.
  const pending = (!readOnly && conv.session?.pendingQuestion) || null;
  const answerQuestion = useAnswerQuestion(props.sessionId);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [questionNotice, setQuestionNotice] = useState<string | null>(null);
  useEffect(() => {
    if (pending) setQuestionNotice(null);
  }, [pending]);
  const busy = conv.liveRunIds.some((id) => id !== pending?.runId);
  const lastUser = [...conv.turns].reverse().find((turn) => turn.kind === "user");

  // request_decision approvals this chat's run raised (spec §4.2). Pending
  // approvals are `approval:view`-gated: without it nothing is fetched, the
  // list stays empty and the decision is the inbox's alone.
  const may = useMay();
  const { data: pendingApprovals = [] } = useApprovals("pending", {
    enabled: may("approval:view"),
  });
  const taskId = conv.session?.taskId ?? null;
  // Decided here: the pending list drops them, the chat keeps the answer.
  const [decidedHere, setDecidedHere] = useState<Approval[]>([]);
  useEffect(() => {
    setDecidedHere([]);
  }, [props.sessionId]);
  const sessionDecisions = taskId
    ? [
        ...pendingApprovals.filter(
          (a) =>
            a.actionType === "decision" &&
            a.taskId === taskId &&
            !decidedHere.some((d) => d.id === a.id),
        ),
        ...decidedHere.filter((a) => a.taskId === taskId),
      ]
    : [];
  const mayDecide = (approval: Approval) =>
    mayActOn(me?.seats ?? [], me?.decidesAllDepartments === true, approval.departmentId ?? null);
  const lastAssistant = [...conv.turns].reverse().find((turn) => turn.kind === "assistant");
  // Replies are offered only under a finished answer that is the newest turn,
  // and only while the reader could send one.
  const lastTurn = conv.turns[conv.turns.length - 1];
  const latestAnswer =
    lastTurn?.kind === "assistant" && !lastTurn.liveRunId
      ? (lastTurn.variants[lastTurn.variants.length - 1] ?? null)
      : null;
  const offerChips = !!latestAnswer && !busy && !pending && !readOnly;
  const lastMessage = conv.messages[conv.messages.length - 1];
  const suggestions: ReplyChip[] = !offerChips
    ? []
    : latestAnswer.suggestions?.length
      ? latestAnswer.suggestions.map((s) => ({ label: s, text: s }))
      : nextStepChips(
          {
            hasMessages: conv.turns.length > 0,
            lastTurnRole: lastMessage?.role ?? null,
            lastUserMode: [...conv.messages].reverse().find((m) => m.role === "user")?.mode ?? null,
            pendingApprovals: pendingApprovals.length,
            budgetSoftExceeded: false,
            mayViewBudget: false,
            promptStarters: [],
          },
          t,
        ).map((chip) => ({ label: chip.label, text: chip.insert }));

  // Context for the next message (C3): picked from the + menu or with #,
  // and pinned to the chat from its chip.
  const [refs, setRefs] = useState<ContextItem[]>([]);
  const [picker, setPicker] = useState<null | "source" | "knowledge" | "object">(null);
  const [pinLimit, setPinLimit] = useState(false);
  const pins = conv.session?.pinnedRefs ?? [];
  const setPins = useSetPins(props.sessionId);
  const mayKnowledge = may("knowledge:view");
  const resetPins = setPins.reset;
  useEffect(() => {
    // Another chat's refusal is not this one's.
    setPinLimit(false);
    resetPins();
  }, [props.sessionId, resetPins]);
  const plusItems: PlusMenuItem[] = readOnly
    ? []
    : [
        ...(mayKnowledge
          ? [
              {
                id: "source",
                label: t("Choose from data source ›", "Aus Datenquelle wählen ›"),
                icon: <Database className="size-4" aria-hidden />,
                onSelect: () => setPicker("source"),
              },
              {
                id: "knowledge",
                label: t("Knowledge base", "Wissensbasis"),
                icon: <BookOpen className="size-4" aria-hidden />,
                onSelect: () => setPicker("knowledge"),
              },
            ]
          : []),
        {
          id: "object",
          label: t("oc8 object", "oc8-Objekt"),
          icon: <Bot className="size-4" aria-hidden />,
          onSelect: () => setPicker("object"),
        },
      ];
  function addRef(item: ContextItem) {
    setRefs((prev) =>
      prev.some((r) => r.kind === item.kind && r.id === item.id) || prev.length >= MAX_MESSAGE_REFS
        ? prev
        : [...prev, item],
    );
    setPicker(null);
  }
  function dropRef(ref: ContextItem) {
    setRefs((prev) => prev.filter((r) => !(r.kind === ref.kind && r.id === ref.id)));
  }
  /** "Pin to this chat": the chip leaves the message once the chat holds it. */
  function pinRef(ref: ContextItem) {
    setPinLimit(false);
    if (pins.some((p) => p.kind === ref.kind && p.id === ref.id)) {
      dropRef(ref);
      return;
    }
    if (pins.length >= MAX_PINS) {
      setPinLimit(true);
      return;
    }
    setPins.mutate(
      [...pins.map(({ kind, id }) => ({ kind, id })), { kind: ref.kind, id: ref.id }],
      { onSuccess: () => dropRef(ref) },
    );
  }
  const refusedLabels = refusedRefs(setPins.error).map(
    (r) => [...pins, ...refs].find((x) => x.kind === r.kind && x.id === r.id)?.label ?? r.id,
  );
  const pinError = pinLimit
    ? t(
        `A chat holds up to ${MAX_PINS} pins. Unpin one first.`,
        `Ein Chat hält bis zu ${MAX_PINS} angeheftete Einträge. Löse zuerst einen.`,
      )
    : !setPins.error
      ? null
      : refusedLabels.length > 0
        ? t(
            `Not available to you: ${refusedLabels.join(", ")}. Unpin it, then try again.`,
            `Für dich nicht verfügbar: ${refusedLabels.join(", ")}. Löse es und versuch es noch einmal.`,
          )
        : t(
            "Your pins were not saved. Try again.",
            "Deine angehefteten Einträge wurden nicht gespeichert. Versuch es noch einmal.",
          );
  const pinnedAbove =
    props.sessionId && (pins.length > 0 || pinError) ? (
      <>
        <PinnedBar sessionId={props.sessionId} pins={pins} readOnly={readOnly} />
        {pinError && (
          <p role="alert" className="px-1 text-xs text-status-error">
            {pinError}
          </p>
        )}
      </>
    ) : null;

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
              pendingQuestion: null,
              taskId: null,
              pinnedRefs: [],
              projectId: session.projectId ?? null,
              pinned: session.pinned ?? false,
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
    if (pending && props.sessionId) {
      await answerFreely(pending.clarificationId, submit.text, fromComposer);
      return;
    }
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

  /** While a question is open, what the composer sends is its answer. */
  async function answerFreely(clarificationId: string, text: string, fromComposer: boolean) {
    try {
      await answerQuestion.mutateAsync({ clarificationId, text });
    } catch (error) {
      const status = error instanceof ApiError ? error.status : null;
      if (status === 409) {
        // Someone else answered: what was typed can still go out as a message.
        if (fromComposer) setDraft(text);
        setQuestionNotice(t("Already answered", "Schon beantwortet"));
        return;
      }
      if (status === 404) return;
      if (fromComposer) setDraft(text);
      setSendError(
        status === 422 && error instanceof ApiError && error.message
          ? error.message
          : t(
              "Your answer was not sent. Try again.",
              "Deine Antwort wurde nicht gesendet. Versuch es noch einmal.",
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
        {...assistantExtras(turn)}
      />
    );
  }

  /** This chat's decisions: on the newest answer, or on their own below the
   *  turns while there is no answer yet (never both). */
  function decisionCards(): ReactNode {
    if (sessionDecisions.length === 0) return null;
    return sessionDecisions.map((approval) => (
      <DecisionCard
        key={approval.id}
        approval={approval}
        mayDecide={mayDecide(approval)}
        onDecided={(option) =>
          setDecidedHere((prev) => [
            ...prev.filter((d) => d.id !== approval.id),
            { ...approval, status: "approved", decisionOption: option },
          ])
        }
      />
    ));
  }

  /** The surface's own extras first, then this chat's decisions and replies
   *  on the newest answer. */
  function assistantExtras(turn: AssistantTurnModel): { cards?: ReactNode; footer?: ReactNode } {
    const own = props.renderAssistantExtras?.(turn) ?? {};
    const decisions = turn === lastAssistant ? decisionCards() : null;
    const chips =
      turn === lastTurn && suggestions.length > 0 ? (
        <SuggestionChips
          suggestions={suggestions}
          onSend={sendText}
          onInsert={(text) => setDraft(draft.trim() ? `${draft.trimEnd()} ${text}` : text)}
        />
      ) : null;
    return {
      cards:
        own.cards || decisions ? (
          <>
            {own.cards}
            {decisions}
          </>
        ) : undefined,
      footer:
        own.footer || chips ? (
          <>
            {own.footer}
            {chips}
          </>
        ) : undefined,
    };
  }

  const sendText = (text: string) => {
    if (busy || readOnly) return;
    void onSend({ text, attachmentIds: [], contextRefs: [] }, false);
  };
  const empty =
    typeof props.emptyState === "function" ? props.emptyState(sendText) : props.emptyState;
  const full = props.variant === "full";
  const questionAbove =
    pending && props.sessionId ? (
      dismissed === pending.clarificationId ? (
        <button
          type="button"
          onClick={() => setDismissed(null)}
          className="inline-flex min-h-9 items-center gap-2 rounded-md px-2 text-xs text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <span className="size-2 rounded-full bg-status-warning" aria-hidden />
          {t("Show questions", "Fragen anzeigen")}
        </button>
      ) : (
        <QuestionCard
          key={pending.clarificationId}
          sessionId={props.sessionId}
          pending={pending}
          onClose={(notice) => {
            if (notice) setQuestionNotice(notice);
            setDismissed(pending.clarificationId);
          }}
        />
      )
    ) : questionNotice ? (
      <p role="status" className="text-xs text-muted-foreground">
        {questionNotice}
      </p>
    ) : null;
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
            <>
              {empty ?? null}
              <PromptStarters starters={props.promptStarters ?? []} onPick={sendText} />
            </>
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

          {!conv.isLoading && !lastAssistant && sessionDecisions.length > 0 && (
            <div data-testid="session-decisions" className="space-y-2">
              {decisionCards()}
            </div>
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
          placeholder={
            pending ? t("Or answer freely …", "Oder frei antworten …") : props.placeholder
          }
          modelValue={conv.session?.modelOverrideId ?? null}
          showModelPicker={!!props.sessionId}
          agentItems={props.agentItems}
          onAddressAgent={props.onAddressAgent}
          target={props.composerTarget}
          trailing={props.composerTrailing}
          textOnlyHint={
            pending
              ? t(
                  "Sent as the answer – attachments only afterwards.",
                  "Wird als Antwort gesendet – Anhänge gehen erst danach.",
                )
              : undefined
          }
          above={
            pinnedAbove || questionAbove || props.composerAbove ? (
              <>
                {pinnedAbove}
                {questionAbove}
                {props.composerAbove}
              </>
            ) : undefined
          }
          contextRefs={refs}
          onContextRefsChange={setRefs}
          extraPlusItems={plusItems}
          onPinRef={props.sessionId && !readOnly ? pinRef : undefined}
          disabled={readOnly}
          autoFocus={props.autoFocus}
        />
        {!readOnly && (
          <>
            <DataSourceBrowser
              open={picker === "source"}
              onPick={addRef}
              onClose={() => setPicker(null)}
            />
            <ContextSearchDialog
              open={picker === "knowledge" || picker === "object"}
              kinds={picker === "knowledge" ? ["knowledge_base"] : OBJECT_KINDS}
              title={
                picker === "knowledge"
                  ? t("Knowledge base", "Wissensbasis")
                  : t("oc8 object", "oc8-Objekt")
              }
              onPick={addRef}
              onClose={() => setPicker(null)}
            />
          </>
        )}
      </div>
    </div>
  );
}
