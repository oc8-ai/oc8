// No bubble, full column width. Top to bottom: thought, steps, the answer
// with a caret, cards, sources, then actions (spec §2.1).
import { useEffect, useState, type ReactNode } from "react";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Copy,
  RotateCcw,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";

import { useChatSlot } from "@/edition/composition";
import { MessageActions, MessageParts } from "@/edition/slots";
import { RunComponents } from "@/components/run-record-card";
import { ConnectionRequiredNotice } from "@/components/connections/connection-required-notice";
import { ToolDeniedNotice } from "@/components/connections/tool-denied-notice";
import { RunStartPhase } from "@/components/run-start-phase";
import { useMessageFeedback, useTurnRun } from "@/lib/conversation-api";
import { sourcesOf, type RunActivityDTO } from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { isEmptyReply } from "./empty-reply";
import { Sources } from "./sources";
import { StepList } from "./step-list";
import { StreamingText } from "./streaming-text";
import { ThoughtDisclosure, thoughtOf } from "./thought-disclosure";
import type { AssistantTurnModel } from "./types";
import { useObjectNavigation } from "./use-object-navigation";

const TERMINAL = new Set(["done", "failed", "interrupted"]);

function ActionButton({
  label,
  onClick,
  children,
  pressed,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
  pressed?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={label}
      aria-pressed={pressed}
      className={cn(
        "grid size-9 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        pressed && "text-foreground",
      )}
    >
      {children}
    </button>
  );
}

export function AssistantTurn({
  sessionId,
  turn,
  run: suppliedRun,
  trigger,
  cards,
  footer,
  readOnly = false,
  labelsOnly = false,
  onRegenerate,
}: {
  sessionId: string | null;
  turn: AssistantTurnModel;
  run?: RunActivityDTO;
  trigger?: ReactNode;
  cards?: ReactNode;
  footer?: ReactNode;
  readOnly?: boolean;
  /** A colleague's run: steps as labels only (see StepList). */
  labelsOnly?: boolean;
  onRegenerate?: (replyToId: string) => void;
}) {
  const t = useT();
  const total = turn.variants.length + (turn.liveRunId ? 1 : 0);
  const [picked, setPicked] = useState<number | null>(null);
  // A new answer (a regenerate starting, or one landing) is what the user
  // asked for: show it, even if they had paged back to an older one.
  useEffect(() => {
    setPicked(null);
  }, [total, turn.liveRunId]);
  const index = picked === null ? total - 1 : Math.min(picked, total - 1);
  const showingLive = turn.liveRunId !== null && index === turn.variants.length;
  const message = showingLive ? null : (turn.variants[index] ?? null);
  const runId = showingLive ? turn.liveRunId : (message?.runId ?? null);
  const { data: fetchedRun } = useTurnRun(suppliedRun ? null : sessionId, runId);
  const run = suppliedRun ?? fetchedRun;
  const live = showingLive && !!run && !TERMINAL.has(run.state);
  // A run that is not a chat turn (a timeline run) has no answer message:
  // once it has ended, its output is the answer.
  const answer = message
    ? message.content
    : run?.liveAnswer || (run && TERMINAL.has(run.state) ? (run.output ?? "") : "");
  const thought = run ? thoughtOf(run) : { text: null, seconds: null };
  // Copied and rated belong to the answer on screen, not to the turn.
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [ratings, setRatings] = useState<Record<string, "up" | "down">>({});
  const copied = !!message && copiedId === message.id;
  const rating = message ? (ratings[message.id] ?? null) : null;
  const [highlighted, setHighlighted] = useState<number | null>(null);
  const feedback = useMessageFeedback(sessionId);
  const openObject = useObjectNavigation();
  const sources = sourcesOf(message);
  // An edition that draws `sources` itself replaces the core list.
  const partRenderers = useChatSlot("messageParts");
  const editionSources = partRenderers.some((r) => r.type === "sources");
  const components = message ? message.renderedComponents : (run?.components ?? []);
  const noAnswer = isEmptyReply(message) && !run?.connectionRequired && !run?.toolDenied;

  function copy() {
    if (!message) return;
    const id = message.id;
    // navigator.clipboard is undefined outside a secure context.
    if (!navigator.clipboard) return;
    navigator.clipboard
      .writeText(answer)
      .then(() => {
        setCopiedId(id);
        setTimeout(() => setCopiedId((current) => (current === id ? null : current)), 1500);
      })
      .catch(() => undefined);
  }

  const [feedbackFailed, setFeedbackFailed] = useState<string | null>(null);
  function rate(value: "up" | "down") {
    if (!message) return;
    const id = message.id;
    const before = ratings[id];
    setFeedbackFailed(null);
    setRatings((prev) => ({ ...prev, [id]: value }));
    feedback.mutate(
      { messageId: id, rating: value },
      {
        // A failed save reverts the control and says so (DESIGN.md, Error).
        onError: () => {
          setRatings((prev) => {
            const next = { ...prev };
            if (before) next[id] = before;
            else delete next[id];
            return next;
          });
          setFeedbackFailed(id);
        },
      },
    );
  }

  return (
    <article className="turn-in space-y-2" aria-label={t("Answer", "Antwort")}>
      {trigger}
      {/* An isolated run before its first step: say the workspace is
          starting rather than show nothing (RunStartPhase hides itself
          outside the start phases and once the run has ended). */}
      {run && <RunStartPhase phase={run.phase} state={run.state} />}
      <ThoughtDisclosure live={live && !answer} text={thought.text} seconds={thought.seconds} />
      {run && <StepList run={run} labelsOnly={labelsOnly} />}
      <StreamingText
        text={answer}
        streaming={live}
        sources={sources}
        onObjectClick={openObject}
        onCitation={(n) => {
          setHighlighted(n);
          document.getElementById(`source-${n}`)?.scrollIntoView({ block: "nearest" });
        }}
      />
      {noAnswer && (
        <p role="status" className="text-sm text-muted-foreground">
          {t(
            "The assistant returned no answer. Try again or rephrase.",
            "Der Assistent hat keine Antwort geliefert. Versuch es nochmal oder formuliere anders.",
          )}
        </p>
      )}
      {run?.state === "waiting_for_input" && showingLive && (
        <p className="text-xs text-status-warning" aria-live="polite">
          {t("Waiting for answers…", "Wartet auf Antworten …")}
        </p>
      )}
      {/* A tool call needed an account that is not there: say which, and
          link to where it gets connected. */}
      <ConnectionRequiredNotice value={run?.connectionRequired} agentId={run?.agentId} />
      {/* A tool call the guardrails withhold: say which, and where to allow it. */}
      <ToolDeniedNotice value={run?.toolDenied} />
      <RunComponents components={components} />
      {cards}
      {message && <MessageParts message={message} renderers={partRenderers} />}
      {!editionSources && <Sources sources={sources} highlighted={highlighted} />}
      {footer}
      {!live && (message || total > 1) && (
        <div className="flex items-center gap-1">
          {total > 1 && (
            <span className="inline-flex items-center gap-0.5 text-xs text-muted-foreground">
              <ActionButton
                label={t("Previous answer", "Vorherige Antwort")}
                onClick={() => setPicked(Math.max(0, index - 1))}
              >
                <ChevronLeft className="size-4" />
              </ActionButton>
              <span className="font-mono tabular-nums">
                {index + 1}/{total}
              </span>
              <ActionButton
                label={t("Next answer", "Nächste Antwort")}
                onClick={() => setPicked(Math.min(total - 1, index + 1))}
              >
                <ChevronRight className="size-4" />
              </ActionButton>
            </span>
          )}
          {message && (
            <>
              <ActionButton
                label={copied ? t("Copied", "Kopiert") : t("Copy answer", "Antwort kopieren")}
                onClick={copy}
              >
                {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
              </ActionButton>
              {!readOnly && turn.replyToId && onRegenerate && (
                <ActionButton
                  label={t("Regenerate answer", "Antwort neu erzeugen")}
                  onClick={() => onRegenerate(turn.replyToId as string)}
                >
                  <RotateCcw className="size-4" />
                </ActionButton>
              )}
              {!readOnly && (
                <>
                  <ActionButton
                    label={t("Good answer", "Gute Antwort")}
                    pressed={rating === "up"}
                    onClick={() => rate("up")}
                  >
                    <ThumbsUp className="size-4" />
                  </ActionButton>
                  <ActionButton
                    label={t("Bad answer", "Schlechte Antwort")}
                    pressed={rating === "down"}
                    onClick={() => rate("down")}
                  >
                    <ThumbsDown className="size-4" />
                  </ActionButton>
                </>
              )}
            </>
          )}
          {message && (
            <MessageActions message={message} sessionId={sessionId} readOnly={readOnly} />
          )}
        </div>
      )}
      {message && feedbackFailed === message.id && (
        <p role="alert" className="text-xs text-status-error">
          {t(
            "Your rating was not saved. Try again.",
            "Deine Bewertung wurde nicht gespeichert. Versuch es noch einmal.",
          )}
        </p>
      )}
    </article>
  );
}
