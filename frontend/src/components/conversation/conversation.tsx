// frontend/src/components/conversation/conversation.tsx
// The one chat (spec §2.1). Every surface mounts this; only spacing differs
// between "full" (Copilot page, Live tab) and "compact" (dock, My Work).
import { Fragment, useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { ArrowDown, BookOpen, Bot, CornerDownRight, Database, X } from "lucide-react";

import type { SigilItem } from "@/components/composer-sigils";
import { Skeleton } from "@/components/ui/skeleton";
import { api, ApiError } from "@/lib/api";
import {
  conversationKeys,
  MAX_PINS,
  pinErrorText,
  useAnswerQuestion,
  useRegenerate,
  useRunSteering,
  useSendRunInput,
  useSetChatMode,
  useSetPins,
  useStopRun,
  useTurnRun,
  type ChatSessionDetailDTO,
  type ContextItem,
  type LiveRunDTO,
} from "@/lib/conversation-api";
import { detectCommand, LOCAL_COMMANDS } from "@/lib/chat-commands";
import { nextStepChips } from "@/lib/chat-suggestions";
import { useCan } from "@/lib/governance-hooks";
import { mayActOn, useApprovals, useAuth, useBudgetStatus, type Approval } from "@/lib/hooks";
import {
  appendMessage,
  chatKeys,
  useCreateChatSession,
  useSendChatMessage,
  type ChatMessageDTO,
} from "@/lib/hooks-chat";
import { useMay } from "@/lib/governance-hooks";
import { useLang, useT } from "@/lib/i18n";
import { useWatchRuns } from "@/lib/live/provider";
import { cn } from "@/lib/utils";

import { SidePanelProvider } from "@/edition/slots";
import { AssistantTurn } from "./assistant-turn";
import { Composer, type ComposerSubmit, type PlusMenuItem } from "./composer";
import { ContextSearchDialog } from "./context-search";
import { DataSourceBrowser } from "./data-source-browser";
import { DecisionCard } from "./decision-card";
import { AgentModeChip } from "./agent-mode-chip";
import { PinnedBar } from "./pinned-bar";
import { PromptStarters, type PromptStarter } from "./prompt-starters";
import { QuestionCard } from "./question-card";
import { RunInputs, runInputError } from "./run-steering";
import { SuggestionChips, type ReplyChip } from "./suggestion-chips";
import type { AssistantTurnModel, ConversationVariant, TurnModel } from "./types";
import { useTimelineRuns } from "./timeline-runs";
import { AgentBuilderCard } from "./agent-builder-card";
import { TurnApprovalCards } from "./turn-approval-cards";
import { TURN_APPROVAL_CARDS } from "./turn-approval-registry";
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

/** A message on its way: shown at once as the user's turn with the answer
 *  waiting below it (the breathing mark), until the server's own rows take
 *  over. Those keep the same React keys (`aliases`), so nothing remounts and
 *  the mark does not restart when the run becomes known. */
interface Outbox {
  id: string;
  /** The chat it was sent in; null until a new chat has been created. */
  sessionId: string | null;
  text: string;
  at: string;
  contextRefs: { kind: string; id: string; label: string }[];
  /** The newest message when it was sent: the first user turn after it is
   *  this one, even if a live refetch brings it before the send returns. */
  afterId: string | null;
  /** The stored message, once the send has returned. */
  messageId: string | null;
  /** False when the server stored the message but started no run. */
  expectRun: boolean;
}

let outboxSeq = 0;

/** The stored message an outbox entry became, once the transcript has it. */
function landedMessageId(
  outbox: Outbox,
  messages: ChatMessageDTO[],
  memberId: string | null,
): string | null {
  if (outbox.messageId) {
    return messages.some((m) => m.id === outbox.messageId) ? outbox.messageId : null;
  }
  const start = outbox.afterId ? messages.findIndex((m) => m.id === outbox.afterId) + 1 : 0;
  if (outbox.afterId && start === 0) return null;
  const mine = messages
    .slice(start)
    .find(
      (m) => m.role === "user" && (!memberId || !m.authorMemberId || m.authorMemberId === memberId),
    );
  return mine?.id ?? null;
}

type Translate = (en: string, de?: string) => string;

/** A message that did not go out: what to say, and what "Send again" resends
 *  (none when resending cannot help). */
interface SendFailure {
  text: string;
  retry?: { submit: ComposerSubmit; fromComposer: boolean };
}

/** Why a message did not go out, by the server's answer (Ruling 53). */
function sendErrorText(error: unknown, t: Translate): string {
  const status = error instanceof ApiError ? error.status : null;
  if (status === 403) {
    return t("You may not write to this agent.", "Du darfst diesem Agenten nicht schreiben.");
  }
  if (status === 404) {
    return t(
      "This chat no longer exists. Start a new one.",
      "Diesen Chat gibt es nicht mehr. Starte einen neuen.",
    );
  }
  // The server's own sentence; a validation list is no sentence.
  if (
    status === 422 &&
    error instanceof ApiError &&
    error.message &&
    !/^[[{]/.test(error.message)
  ) {
    return error.message;
  }
  if (status === null || status >= 500) {
    return t(
      "The agent isn't reachable right now. Your message wasn't sent – try again in a moment.",
      "Der Agent ist gerade nicht erreichbar. Deine Nachricht wurde nicht gesendet – versuch es gleich noch einmal.",
    );
  }
  return t(
    "Your message was not sent. Try again.",
    "Deine Nachricht wurde nicht gesendet. Versuch es noch einmal.",
  );
}

function localDay(at: string): string {
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? "" : d.toDateString();
}

export interface ConversationProps {
  agentId: string;
  sessionId: string | null;
  /** A new chat was created (its id), or the open one is gone (null: start
   *  a new one). */
  onSessionChange: (sessionId: string | null) => void;
  variant: ConversationVariant;
  /** Shown while the chat has no turns. A function receives `send`, so
   *  anything in it can go out as the first message. */
  emptyState?: ReactNode | ((send: (text: string) => void) => ReactNode);
  /** Cards under the empty state; picking one sends its title. */
  promptStarters?: PromptStarter[];
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
  /** The composer sends to a running run while the chat is busy (the Live
   *  tab's target): Send stays next to Stop. */
  sendWhileBusy?: boolean;
  /** The run `onSendOverride` writes to; its inputs show under its turn. */
  steeringRunId?: string | null;
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
  /** This is the Copilot's own chat: `/agent` (Agent mode) is offered. It is
   *  the Assistant's alone -- the server refuses it in any other chat. */
  agentMode?: boolean;
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
  return (
    <SidePanelProvider>
      <ConversationBody {...props} />
    </SidePanelProvider>
  );
}

function ConversationBody(props: ConversationProps) {
  const t = useT();
  const qc = useQueryClient();
  const { data: me } = useAuth();
  const { lang } = useLang();
  const conv = useConversation(props.sessionId);
  const [outbox, setOutbox] = useState<Outbox | null>(null);
  // Stored message id -> the outbox id its turn was first shown under.
  const [aliases, setAliases] = useState<Record<string, string>>({});
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
  // The message on its way, until the transcript has it and its answer.
  const sending = outbox && outbox.sessionId === (props.sessionId ?? null) ? outbox : null;
  const landedId = sending ? landedMessageId(sending, conv.messages, me?.memberId ?? null) : null;
  const sentLanded = landedId !== null;
  const answerLanded =
    landedId !== null &&
    conv.turns.some((turn) => turn.kind === "assistant" && turn.replyToId === landedId);
  const keyAliases = sending && landedId ? { ...aliases, [landedId]: sending.id } : aliases;
  if (sending && !sentLanded) {
    rows.push({
      at: sending.at,
      kind: "turn",
      turn: {
        kind: "user",
        key: sending.id,
        message: {
          id: sending.id,
          sessionId: props.sessionId ?? "",
          role: "user",
          content: sending.text,
          runId: null,
          renderedComponents: [],
          createdAt: sending.at,
          attachments: [],
          mode: null,
          contextRefs: sending.contextRefs,
          authorMemberId: me?.memberId ?? null,
          authorName: null,
          replyToId: null,
        },
      },
    });
  }
  if (sending && !answerLanded && (sending.expectRun || !sentLanded)) {
    rows.push({
      at: sending.at,
      kind: "turn",
      turn: {
        kind: "assistant",
        key: `answer-${sending.id}`,
        replyToId: landedId,
        variants: [],
        liveRunId: null,
        pending: true,
      },
    });
  }
  // Done once the send has returned (its key alias is stored) and the
  // transcript shows the message and, if one was started, its answer.
  const outboxDone = !!sending?.messageId && sentLanded && (answerLanded || !sending.expectRun);
  useEffect(() => {
    if (outboxDone) setOutbox(null);
  }, [outboxDone]);
  /** A row's React key: a turn first shown from the outbox keeps its key. */
  function rowKey(row: Row): string {
    if (row.kind === "run") return `extra-${row.extra.run.id}`;
    const turn = row.turn;
    if (turn.kind === "user") return keyAliases[turn.key] ?? turn.key;
    const alias = turn.replyToId ? keyAliases[turn.replyToId] : undefined;
    return alias && turn.key === `answer-${turn.replyToId}` ? `answer-${alias}` : turn.key;
  }
  const [ownDraft, setOwnDraft] = useState("");
  const draft = props.draft ?? ownDraft;
  const setDraft = props.onDraftChange ?? setOwnDraft;
  const [sendError, setSendError] = useState<SendFailure | null>(null);
  // An error belongs to the chat it happened in; a chat that vanished keeps
  // its notice on the new chat that replaces it.
  const keepError = useRef(false);
  useEffect(() => {
    if (keepError.current) keepError.current = false;
    else setSendError(null);
  }, [props.sessionId]);
  const [showBudget, setShowBudget] = useState(false);
  const mayViewBudget = useCan()("budget:view");
  // Also fetched once a conversation is under way, so the "budget is nearly
  // used" reply chip can appear before anyone types /budget.
  const { data: budget } = useBudgetStatus(null, {
    enabled: showBudget || (mayViewBudget && conv.turns.length > 0),
  });
  const createSession = useCreateChatSession();
  const sendMessage = useSendChatMessage(props.sessionId ?? "");
  const regenerate = useRegenerate(props.sessionId);
  const stopRun = useStopRun();
  // Any new row re-checks the bottom; an older page landing on top moves
  // only a reader already there (the anchoring below keeps everyone else).
  const newest = rows[rows.length - 1];
  const newestKey = newest ? rowKey(newest) : "";
  const { ref, contentRef, atBottom, scrollToBottom, followFromHere } = useScrollAnchor(
    `${newestKey}:${rows.length}:${conv.liveRunIds.join()}:${sendError?.text ?? ""}`,
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
  // Ruling 52: while the reader's own answer is being written, what they
  // type goes to that run (POST /runs/{id}/inputs). A surface that routes
  // the composer itself (the Live tab, to a timeline run) goes first; its
  // own chat answers are never on that timeline, so they are steered here.
  const replyRunId = !readOnly
    ? ([...conv.liveRunIds].reverse().find((id) => id !== pending?.runId) ?? null)
    : null;
  const { data: replySteering } = useRunSteering(replyRunId);
  const sendReplyInput = useSendRunInput(replyRunId);
  const steerReply =
    !!replyRunId && !!replySteering?.mayWrite && replySteering.acceptsInputs && busy;
  // Ruling 51: a new chat's model is chosen before its first message. The
  // pick is held here and written right after the chat exists, before the
  // first message goes out (one after the other, never racing).
  const [heldModel, setHeldModel] = useState<string | null | undefined>(undefined);
  const heldModelRef = useRef(heldModel);
  heldModelRef.current = heldModel;
  const [modelScope, setModelScope] = useState(props.sessionId);
  if (modelScope !== props.sessionId) {
    setModelScope(props.sessionId);
    // The new chat being created under the pick keeps it.
    if (modelScope !== null) setHeldModel(undefined);
  }
  // Agent mode (Copilot Agent Builder §2). A chat that does not exist yet holds
  // the mode here and its first message carries "/agent", so no empty chat is
  // created just to switch (Ruling 57's reasoning).
  const setChatMode = useSetChatMode(props.sessionId);
  const [heldMode, setHeldMode] = useState<string | null>(null);
  const [modeScope, setModeScope] = useState(props.sessionId);
  if (modeScope !== props.sessionId) {
    setModeScope(props.sessionId);
    // Once the chat exists the server is the truth.
    setHeldMode(null);
  }
  // Never carry a refusal over to another chat (reset outside render).
  const resetChatMode = setChatMode.reset;
  useEffect(() => resetChatMode(), [props.sessionId, resetChatMode]);
  const chatMode = props.sessionId ? (conv.session?.mode ?? null) : heldMode;
  function enterMode(key: string) {
    if (props.sessionId) setChatMode.mutate(key);
    else setHeldMode(key);
  }
  function leaveMode() {
    // Held, the mode is not on the server yet; once the chat exists (its first
    // message carried "/agent") leaving must reach the server too.
    if (props.sessionId) setChatMode.mutate(null);
    else setHeldMode(null);
  }
  // The server may refuse a switch (e.g. out of /plan or /ask): its sentence is
  // shown and the chat keeps the mode it had.
  const modeError = setChatMode.error
    ? setChatMode.error instanceof ApiError &&
      setChatMode.error.message &&
      !/^[[{]/.test(setChatMode.error.message)
      ? setChatMode.error.message
      : t("The mode could not be changed.", "Der Modus konnte nicht geändert werden.")
    : null;
  const modeAbove =
    (chatMode === "agent" && !readOnly) || modeError ? (
      <>
        {chatMode === "agent" && !readOnly && (
          <AgentModeChip onLeave={leaveMode} busy={setChatMode.isPending} />
        )}
        {modeError && (
          <p role="alert" className="px-1 text-xs text-status-error">
            {modeError}
          </p>
        )}
      </>
    ) : null;
  const firstMessage =
    !readOnly && (!props.sessionId || (!conv.isLoading && conv.turns.length === 0 && !busy));
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
  // A failed answer offers no next steps: there is nothing to build on.
  const { data: latestRun } = useTurnRun(props.sessionId, latestAnswer?.runId ?? null);
  const offerChips =
    !!latestAnswer && !busy && !pending && !readOnly && latestRun?.state !== "failed";
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
            budgetSoftExceeded: !!budget?.softExceeded,
            mayViewBudget,
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
  // The chat's one pin writer, shared by the pinned bar and the chips.
  const pinning = useSetPins(props.sessionId);
  const mayKnowledge = may("knowledge:view");
  const resetPins = pinning.reset;
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
                label: t("Knowledge base", "Wissensdatenbank"),
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
    pinning.mutate({ add: { kind: ref.kind, id: ref.id } }, { onSuccess: () => dropRef(ref) });
  }
  const pinError = pinLimit
    ? t(
        `A chat holds up to ${MAX_PINS} pins. Unpin one first.`,
        `Ein Chat hält bis zu ${MAX_PINS} angeheftete Einträge. Löse zuerst einen.`,
      )
    : pinErrorText(pinning.error, [...pins, ...refs], t);
  // The + menu item that opened a picker is gone once it closes.
  const composerArea = useRef<HTMLDivElement>(null);
  const focusComposer = () => composerArea.current?.querySelector("textarea")?.focus();
  // An answered question's card unmounts with the focus inside it: the
  // reader carries on in the composer instead of at the top of the page.
  const pendingId = pending?.clarificationId ?? null;
  const lastPending = useRef(pendingId);
  useEffect(() => {
    const had = lastPending.current;
    lastPending.current = pendingId;
    if (
      had &&
      had !== pendingId &&
      (!document.activeElement || document.activeElement === document.body)
    ) {
      focusComposer();
    }
  }, [pendingId]);
  const pinnedAbove =
    props.sessionId && (pins.length > 0 || pinError) ? (
      <>
        <PinnedBar key={props.sessionId} pins={pins} pinning={pinning} readOnly={readOnly} />
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
              mode: heldMode,
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
        // The message on its way belongs to the chat just made.
        setOutbox((prev) =>
          prev && prev.sessionId === null ? { ...prev, sessionId: session.id } : prev,
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

  /** Ruling 51: write the model held for a new chat, before its first
   *  message. A refused model is dropped, so the next send goes with Auto. */
  async function applyHeldModel(sessionId: string) {
    const held = heldModelRef.current;
    if (held === undefined) return;
    heldModelRef.current = undefined;
    setHeldModel(undefined);
    const session = await api.put<ChatSessionDetailDTO>(
      `/chat/sessions/${encodeURIComponent(sessionId)}/model`,
      { modelConfigId: held },
    );
    qc.setQueryData<ChatSessionDetailDTO>(conversationKeys.session(sessionId), (prev) =>
      prev ? { ...prev, modelOverrideId: session.modelOverrideId } : prev,
    );
  }

  /** `fromComposer` false: a prompt starter, which leaves the draft alone. */
  async function onSend(submit: ComposerSubmit, fromComposer = true) {
    followFromHere();
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
    if (steerReply && replyRunId) {
      try {
        await sendReplyInput.mutateAsync(submit.text);
      } catch (error) {
        if (fromComposer) setDraft(submit.text);
        setSendError({ text: runInputError(error, t) });
      }
      return;
    }
    outboxSeq += 1;
    const outboxId = `outbox-${outboxSeq}`;
    setOutbox({
      id: outboxId,
      sessionId: props.sessionId ?? null,
      text: submit.text,
      at: new Date().toISOString(),
      contextRefs: submit.contextRefs.map(({ kind, id, label }) => ({ kind, id, label })),
      afterId: conv.messages[conv.messages.length - 1]?.id ?? null,
      messageId: null,
      expectRun: true,
    });
    const body = {
      message:
        heldMode && !props.sessionId && !submit.text.startsWith("/")
          ? `/${heldMode} ${submit.text}`
          : submit.text,
      attachmentIds: submit.attachmentIds,
      contextRefs: submit.contextRefs.map(({ kind, id }) => ({ kind, id })),
    };
    let modelRefused = false;
    try {
      const sessionId = await ensureSession();
      if (heldMode && !props.sessionId && submit.text.startsWith("/")) {
        // No "/agent" prefix goes out, so the server's chat is not in the mode.
        qc.setQueryData<ChatSessionDetailDTO>(conversationKeys.session(sessionId), (prev) =>
          prev ? { ...prev, mode: null } : prev,
        );
      }
      await applyHeldModel(sessionId).catch((error: unknown) => {
        modelRefused = true;
        throw error;
      });
      let message: ChatMessageDTO;
      if (sessionId === props.sessionId) {
        message = await sendMessage.mutateAsync(body);
      } else {
        // `sendMessage` is bound to the id this render had (none yet).
        message = await api.post<ChatMessageDTO>(
          `/chat/sessions/${encodeURIComponent(sessionId)}/messages`,
          body,
        );
        qc.setQueryData<ChatMessageDTO[]>(chatKeys.messages(sessionId), (prev) =>
          appendMessage(prev, message),
        );
      }
      setAliases((prev) => ({ ...prev, [message.id]: outboxId }));
      setOutbox((prev) =>
        prev?.id === outboxId
          ? { ...prev, messageId: message.id, expectRun: !!message.runId }
          : prev,
      );
      if (message.runId) {
        const run = { runId: message.runId, replyToId: message.id, state: "queued" };
        qc.setQueryData<ChatSessionDetailDTO>(conversationKeys.session(sessionId), (prev) =>
          prev && !prev.activeRuns.some((r) => r.runId === run.runId)
            ? { ...prev, activeRuns: [...prev.activeRuns, run] }
            : prev,
        );
      }
    } catch (error) {
      setOutbox((prev) => (prev?.id === outboxId ? null : prev));
      if (fromComposer) setDraft(submit.text);
      // What was picked for this message stays picked for the next try.
      if (fromComposer) setRefs(submit.contextRefs);
      const status = error instanceof ApiError ? error.status : null;
      if (modelRefused) {
        setSendError({
          text: t(
            "The model you picked isn't available for this chat. Your message wasn't sent – pick another model or send with Auto.",
            "Das gewählte Modell ist für diesen Chat nicht freigegeben. Deine Nachricht wurde nicht gesendet – wähl ein anderes Modell oder sende mit Auto.",
          ),
          retry: { submit, fromComposer },
        });
        return;
      }
      const gone = status === 404 && !!props.sessionId;
      setSendError({
        text: sendErrorText(error, t),
        // Resending cannot help a refusal or a rejected message. A chat that
        // is gone loses its uploads with it.
        retry:
          status === 403 || status === 422
            ? undefined
            : { submit: gone ? { ...submit, attachmentIds: [] } : submit, fromComposer },
      });
      if (gone) {
        keepError.current = true;
        props.onSessionChange(null);
      }
    }
  }

  /** "Send again": the restored draft (or the starter that failed) once more. */
  function resend(failure: SendFailure) {
    if (!failure.retry) return;
    const { submit, fromComposer } = failure.retry;
    if (!fromComposer) {
      void onSend(submit, false);
      return;
    }
    const text = draft.trim();
    if (!text) return;
    setRefs([]);
    void onSend({ text, attachmentIds: submit.attachmentIds, contextRefs: refs });
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
      setSendError({
        text:
          status === 422 && error instanceof ApiError && error.message
            ? error.message
            : t(
                "Your answer was not sent. Try again.",
                "Deine Antwort wurde nicht gesendet. Versuch es noch einmal.",
              ),
      });
    }
  }

  async function onStop() {
    if (props.onStopOverride) {
      props.onStopOverride();
      return;
    }
    for (const runId of conv.liveRunIds) {
      try {
        await stopRun.mutateAsync(runId);
      } catch (error) {
        // 409: it had already finished, which is what Stop wanted.
        if (error instanceof ApiError && error.status === 409) continue;
        setSendError({
          text: t(
            "The answer was not stopped. Try again.",
            "Die Antwort wurde nicht gestoppt. Versuch es noch einmal.",
          ),
        });
      }
    }
  }

  function onRegenerate(replyToId: string) {
    setSendError(null);
    regenerate.mutate(replyToId, {
      onError: (error) =>
        setSendError({
          text:
            error instanceof ApiError && error.status === 409
              ? t(
                  "An answer is still being written. Try again once it's done.",
                  "Es wird noch eine Antwort geschrieben. Versuch es noch einmal, wenn sie fertig ist.",
                )
              : t(
                  "No new answer was started. Try again.",
                  "Es wurde keine neue Antwort gestartet. Versuch es noch einmal.",
                ),
        }),
    });
  }

  /** A local command (/budget) is answered here, never sent as a message. */
  function onLocalCommand(key: string) {
    if (key === "budget") setShowBudget(true);
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
        onRegenerate={busy ? undefined : onRegenerate}
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
    // What the reader wrote to this answer's run while it ran. Only the run
    // being steered is fetched; an earlier one keeps what was loaded.
    const turnRunId = turn.liveRunId ?? turn.variants[turn.variants.length - 1]?.runId ?? null;
    const steered = props.steeringRunId ?? replyRunId;
    const inputs = turnRunId ? (
      <RunInputs
        runId={turnRunId}
        runState={turn.liveRunId ? "running" : "done"}
        enabled={turnRunId === steered}
      />
    ) : null;
    const decisions = turn === lastAssistant ? decisionCards() : null;
    // Other approvals this turn's run is held on, each through its registered
    // card (TURN_APPROVAL_CARDS); a turn of nobody's registry renders nothing.
    const approvals =
      turnRunId && Object.keys(TURN_APPROVAL_CARDS).length > 0 ? (
        <TurnApprovalCards runId={turnRunId} live={!!turn.liveRunId} />
      ) : null;
    // The builder card (Copilot Agent Builder §3.3) rides on the answer that
    // proposed it, as a core part; editions never render it (CORE_PART_TYPES).
    const shown = turn.variants[turn.variants.length - 1];
    const builderPart = shown?.parts?.find((p) => p.type === "agent_builder");
    const builder =
      typeof builderPart?.proposalId === "string" ? (
        <AgentBuilderCard
          proposalId={builderPart.proposalId}
          readOnly={readOnly}
          onPropose={sendText}
        />
      ) : null;
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
        builder || approvals || own.cards || decisions ? (
          <>
            {builder}
            {approvals}
            {own.cards}
            {decisions}
          </>
        ) : undefined,
      footer:
        own.footer || chips || inputs ? (
          <>
            {inputs}
            {own.footer}
            {chips}
          </>
        ) : undefined,
    };
  }

  const sendText = (text: string) => {
    if (readOnly) return;
    // A chip's command can be a local one ("Budget is running low" opens
    // the budget card): it never becomes a message or a run.
    const local = detectCommand(text.trim(), LOCAL_COMMANDS);
    if (local?.command.local) {
      onLocalCommand(local.command.key);
      return;
    }
    if (busy) return;
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
            // Focus was inside the card that is going away.
            requestAnimationFrame(focusComposer);
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
              <Fragment key={rowKey(row)}>
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
            <div role="alert" className="flex flex-wrap items-center gap-2 text-xs">
              <span className="text-status-error">{sendError.text}</span>
              {sendError.retry && (
                <button
                  type="button"
                  onClick={() => resend(sendError)}
                  className="inline-flex min-h-9 items-center rounded-md border border-border px-2 font-medium text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  {t("Send again", "Erneut senden")}
                </button>
              )}
            </div>
          )}
        </div>
      </div>
      <div
        ref={composerArea}
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
                aria-label={t("Close budget", "Budget schließen")}
                className="-mr-2 grid size-11 place-items-center rounded-md text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:size-9"
              >
                <X className="size-3.5" aria-hidden />
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
          onLocalCommand={onLocalCommand}
          onEnterMode={props.agentMode ? enterMode : undefined}
          placeholder={
            pending ? t("Or answer freely …", "Oder frei antworten …") : props.placeholder
          }
          modelValue={heldModel !== undefined ? heldModel : (conv.session?.modelOverrideId ?? null)}
          showModelPicker
          agentId={props.agentId}
          onModelPick={firstMessage ? setHeldModel : undefined}
          sendWhileBusy={steerReply || !!props.sendWhileBusy}
          agentItems={props.agentItems}
          onAddressAgent={props.onAddressAgent}
          target={
            props.composerTarget ??
            (steerReply ? (
              <span className="inline-flex items-center gap-1">
                <CornerDownRight className="size-3.5" aria-hidden />
                {t("Goes to the running answer", "Geht an die laufende Antwort")}
              </span>
            ) : undefined)
          }
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
            modeAbove || pinnedAbove || questionAbove || props.composerAbove ? (
              <>
                {modeAbove}
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
          pinBusy={pinning.isPending}
          disabled={readOnly}
          autoFocus={props.autoFocus}
        />
        {!readOnly && (
          <>
            <DataSourceBrowser
              open={picker === "source"}
              onPick={addRef}
              onClose={() => setPicker(null)}
              returnFocus={focusComposer}
            />
            <ContextSearchDialog
              open={picker === "knowledge" || picker === "object"}
              kinds={picker === "knowledge" ? ["knowledge_base"] : OBJECT_KINDS}
              title={
                picker === "knowledge"
                  ? t("Knowledge base", "Wissensdatenbank")
                  : t("oc8 object", "oc8-Objekt")
              }
              onPick={addRef}
              onClose={() => setPicker(null)}
              returnFocus={focusComposer}
            />
          </>
        )}
      </div>
    </div>
  );
}
