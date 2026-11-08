// frontend/src/components/conversation/question-card.tsx
// Structured questions from ask_user, above the composer (spec §4.1): one
// question at a time, "1 of 4" with arrows, picked with the mouse or the keys.
// Once answered, the thread keeps a one-line QuestionSummary (user-turn.tsx).
import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { ArrowUp, ChevronLeft, ChevronRight, X } from "lucide-react";

import { ApiError } from "@/lib/api";
import {
  useAnswerQuestion,
  type PendingQuestionDTO,
  type QuestionAnswers,
  type QuestionDTO,
} from "@/lib/conversation-api";
import type { ChatAnswersPart } from "@/lib/hooks-chat";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

/** The row key of "Other answer". Never sent: its text is. */
const OTHER = "__other__";
/** The server's cap on a free-text answer. */
const MAX_TEXT = 2000;
const FOCUS_RING =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";

/** The one line an answered card leaves in the thread. */
export function QuestionSummary({
  questions,
  answers,
}: {
  questions: ChatAnswersPart["questions"];
  answers: QuestionAnswers;
}) {
  const parts = questions
    .filter((q) => answers[q.id] !== undefined)
    .map((q) => {
      const value = answers[q.id];
      const text = Array.isArray(value)
        ? value.map((k) => q.options.find((o) => o.key === k)?.label ?? k).join(", ")
        : value;
      return { id: q.id, prompt: q.prompt, text };
    });
  return (
    <p className="text-sm">
      {parts.map((p, i) => (
        <span key={p.id}>
          {i > 0 && <span className="text-muted-foreground"> · </span>}
          <span className="text-muted-foreground">{p.prompt} </span>
          <span className="font-medium">{p.text}</span>
        </span>
      ))}
    </p>
  );
}

function isTyping(target: EventTarget): boolean {
  const tag = (target as HTMLElement).tagName;
  return tag === "INPUT" || tag === "TEXTAREA";
}

export function QuestionCard({
  sessionId,
  pending,
  onClose,
}: {
  sessionId: string;
  pending: PendingQuestionDTO;
  /** `notice`: why the card went away on its own ("Already answered"). */
  onClose: (notice?: string) => void;
}) {
  const t = useT();
  const answer = useAnswerQuestion(sessionId);
  const titleId = useId();
  // A plain question (no structured ones) is one free-text question.
  const plain = pending.questions.length === 0;
  const questions: QuestionDTO[] = plain
    ? [
        {
          id: "plain",
          prompt: pending.text,
          kind: "text",
          options: [],
          allowOther: false,
          required: true,
        },
      ]
    : pending.questions;
  const [index, setIndex] = useState(0);
  const [picked, setPicked] = useState<Record<string, string[]>>({});
  const [free, setFree] = useState<Record<string, string>>({});
  const [active, setActive] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  // Focus follows the roving option only after a key moved it.
  const moved = useRef(false);
  // Set from the send until the server answers: a second Enter is no second post.
  const sending = useRef(false);

  const q = questions[Math.min(index, questions.length - 1)];
  const rows =
    q.kind === "text" ? [] : [...q.options.map((o) => o.key), ...(q.allowOther ? [OTHER] : [])];
  const chosen = picked[q.id] ?? [];
  const selected =
    chosen.filter((k) => k !== OTHER).length +
    (chosen.includes(OTHER) && (free[q.id] ?? "").trim() ? 1 : 0);

  useEffect(() => {
    if (!moved.current) return;
    moved.current = false;
    optionRefs.current[active]?.focus();
  }, [active, index]);

  function toggle(key: string) {
    setError(null);
    const current = picked[q.id] ?? [];
    let next: string[];
    if (q.kind !== "multi") next = [key];
    else if (current.includes(key)) next = current.filter((k) => k !== key);
    // The server takes a list of keys OR one text, never both: an own answer
    // replaces the ticks, and a tick replaces the own answer -- visibly.
    else if (key === OTHER) next = [OTHER];
    else next = [...current.filter((k) => k !== OTHER), key];
    if (q.kind === "multi" && current.includes(OTHER) && !next.includes(OTHER)) {
      setFree((p) => ({ ...p, [q.id]: "" }));
    }
    setPicked((prev) => ({ ...prev, [q.id]: next }));
  }

  function collect(): QuestionAnswers {
    const out: QuestionAnswers = {};
    for (const item of questions) {
      const keys = picked[item.id] ?? [];
      const text = (free[item.id] ?? "").trim();
      if (item.kind === "text") {
        if (text) out[item.id] = text;
      } else if (keys.includes(OTHER) && text) {
        out[item.id] = text;
      } else {
        const options = keys.filter((k) => k !== OTHER);
        if (options.length > 0) out[item.id] = options;
      }
    }
    return out;
  }

  function send() {
    const answers = collect();
    const missing = questions.find((item) => item.required && answers[item.id] === undefined);
    if (missing) {
      setIndex(questions.indexOf(missing));
      setActive(0);
      setError(t("This question needs an answer.", "Diese Frage braucht eine Antwort."));
      return;
    }
    if (sending.current) return;
    setError(null);
    const body = plain
      ? { clarificationId: pending.clarificationId, text: answers.plain as string }
      : { clarificationId: pending.clarificationId, answers };
    sending.current = true;
    answer.mutate(body, {
      onSuccess: () => {
        sending.current = false;
      },
      onError: (e: unknown) => {
        sending.current = false;
        const status = e instanceof ApiError ? e.status : null;
        if (status === 409) onClose(t("Already answered", "Schon beantwortet"));
        else if (status === 404) onClose();
        else if (status === 422 && e instanceof ApiError && e.message) setError(e.message);
        else
          setError(
            t(
              "Your answer was not sent. Try again.",
              "Deine Antwort wurde nicht gesendet. Versuch es noch einmal.",
            ),
          );
      },
    });
  }

  function go(delta: number) {
    setIndex((i) => Math.min(questions.length - 1, Math.max(0, i + delta)));
    setActive(0);
  }

  function onKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    if (e.key === "Enter") {
      e.preventDefault();
      // A held key, an IME composing, or an answer on its way: no second send.
      if (e.repeat || e.nativeEvent.isComposing || answer.isPending || sending.current) return;
      if (index < questions.length - 1) go(1);
      else send();
      return;
    }
    // Typing in a field stays typing: digits, Space and arrows are text there.
    if (isTyping(e.target) || rows.length === 0) return;
    if (/^[1-9]$/.test(e.key)) {
      const n = Number(e.key) - 1;
      if (rows[n]) {
        e.preventDefault();
        setActive(n);
        toggle(rows[n]);
      }
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      moved.current = true;
      setActive((a) => (a + (e.key === "ArrowDown" ? 1 : -1) + rows.length) % rows.length);
    } else if (e.key === " ") {
      e.preventDefault();
      toggle(rows[active] ?? rows[0]);
    } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      moved.current = true;
      go(e.key === "ArrowRight" ? 1 : -1);
    }
  }

  const freeField = (label: string, autoFocus: boolean) => (
    <input
      aria-label={label}
      autoFocus={autoFocus}
      maxLength={MAX_TEXT}
      value={free[q.id] ?? ""}
      onChange={(e) => {
        setError(null);
        setFree((p) => ({ ...p, [q.id]: e.target.value }));
      }}
      className={cn(
        "h-9 w-full rounded-md border border-border bg-background px-2 text-sm",
        FOCUS_RING,
      )}
    />
  );

  const options = rows.map((key, i) => {
    const option = q.options.find((o) => o.key === key);
    const checked = chosen.includes(key);
    const labelId = `${titleId}-${q.id}-${i}`;
    return (
      <div key={key}>
        <button
          ref={(el) => {
            optionRefs.current[i] = el;
          }}
          type="button"
          role={q.kind === "multi" ? "checkbox" : "radio"}
          aria-checked={checked}
          aria-labelledby={labelId}
          aria-describedby={option?.detail ? `${labelId}-detail` : undefined}
          tabIndex={i === active ? 0 : -1}
          // Space is handled on keydown; Firefox would also click on keyup.
          onKeyUp={(e) => {
            if (e.key === " ") e.preventDefault();
          }}
          onClick={() => {
            setActive(i);
            toggle(key);
          }}
          className={cn(
            "flex min-h-11 w-full items-center gap-2 rounded-md px-2 text-left text-sm hover:bg-muted",
            i === active && "bg-muted",
            FOCUS_RING,
          )}
        >
          <span
            aria-hidden
            className={cn(
              "size-4 shrink-0 border border-border",
              q.kind === "multi" ? "rounded-md" : "rounded-full",
              checked && "border-primary bg-primary",
            )}
          />
          <span aria-hidden className="w-3 font-mono text-xs text-muted-foreground">
            {i + 1}
          </span>
          <span className="min-w-0 flex-1">
            <span id={labelId}>{option ? option.label : t("Other answer", "Andere Antwort")}</span>
            {option?.detail && (
              <span id={`${labelId}-detail`} className="block text-xs text-muted-foreground">
                {option.detail}
              </span>
            )}
          </span>
        </button>
        {key === OTHER && checked && (
          <div className="mt-1 pl-2">{freeField(t("Other answer", "Andere Antwort"), true)}</div>
        )}
      </div>
    );
  });

  return (
    <section
      aria-label={t("Questions", "Fragen")}
      className="turn-in rounded-lg border border-border bg-card p-3"
    >
      <header className="mb-2 flex items-start gap-2">
        <h3
          id={titleId}
          className="min-w-0 flex-1 whitespace-pre-wrap pt-1 text-base font-semibold"
        >
          {q.prompt}
        </h3>
        {questions.length > 1 && (
          <span className="inline-flex shrink-0 items-center gap-1 text-xs text-muted-foreground">
            <button
              type="button"
              aria-label={t("Previous question", "Vorherige Frage")}
              onClick={() => go(-1)}
              disabled={index === 0}
              className={cn(
                "grid size-9 place-items-center rounded-md hover:bg-muted disabled:opacity-50",
                FOCUS_RING,
              )}
            >
              <ChevronLeft className="size-4" aria-hidden />
            </button>
            <span aria-live="polite">
              {t(`${index + 1} of ${questions.length}`, `${index + 1} von ${questions.length}`)}
            </span>
            <button
              type="button"
              aria-label={t("Next question", "Nächste Frage")}
              onClick={() => go(1)}
              disabled={index === questions.length - 1}
              className={cn(
                "grid size-9 place-items-center rounded-md hover:bg-muted disabled:opacity-50",
                FOCUS_RING,
              )}
            >
              <ChevronRight className="size-4" aria-hidden />
            </button>
          </span>
        )}
        <button
          type="button"
          aria-label={t("Close questions", "Fragen schließen")}
          onClick={() => onClose()}
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground",
            FOCUS_RING,
          )}
        >
          <X className="size-4" aria-hidden />
        </button>
      </header>
      <div role="group" aria-labelledby={titleId} onKeyDown={onKeyDown} className="space-y-1">
        {q.kind === "text" ? (
          freeField(t("Your answer", "Deine Antwort"), false)
        ) : q.kind === "single" ? (
          <div role="radiogroup" aria-labelledby={titleId} className="space-y-1">
            {options}
          </div>
        ) : (
          options
        )}
        {q.kind === "multi" && q.allowOther && (
          <p className="px-2 text-xs text-muted-foreground">
            {t(
              "Your own answer replaces the ticked options.",
              "Eine eigene Antwort ersetzt die angekreuzten Optionen.",
            )}
          </p>
        )}
      </div>
      <footer className="mt-2 flex items-center gap-2">
        <span className="flex-1 text-xs text-muted-foreground">
          {q.kind !== "text" && t(`${selected} selected`, `${selected} ausgewählt`)}
        </span>
        {error && (
          <span role="alert" className="text-xs text-status-error">
            {error}
          </span>
        )}
        <button
          type="button"
          aria-label={t("Send answers", "Antworten senden")}
          onClick={send}
          disabled={answer.isPending}
          className={cn(
            "grid size-9 shrink-0 place-items-center rounded-md bg-primary text-primary-foreground disabled:opacity-50",
            FOCUS_RING,
          )}
        >
          <ArrowUp className="size-4" aria-hidden />
        </button>
      </footer>
    </section>
  );
}
