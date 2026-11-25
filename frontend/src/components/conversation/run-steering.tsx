// Writing to a running run from the chat (spec §3.2): the composer's target
// line while a visible run is active, the inputs written to a run with what
// became of each, and the words for a refused input.
import { useState, type ReactNode } from "react";
import { CornerDownRight } from "lucide-react";

import { ApiError } from "@/lib/api";
import {
  useCancelRunInput,
  useEditRunInput,
  useRunSteering,
  type RunInputDTO,
  type RunSteeringDTO,
} from "@/lib/conversation-api";
import { useT } from "@/lib/i18n";

const TERMINAL = new Set(["done", "failed", "interrupted"]);

type Translate = (en: string, de?: string) => string;

/** What the composer's target line says while a run is active, and whether
 *  the composer's text goes to the run (`route`) instead of the chat. */
// eslint-disable-next-line react-refresh/only-export-components -- shared helper, used beside the component
export function steeringTarget(
  steering: RunSteeringDTO | undefined,
  t: Translate,
): { target: ReactNode; route: boolean } {
  if (!steering || !steering.mayWrite) return { target: null, route: false };
  if (!steering.acceptsInputs) {
    return {
      target: (
        <span>
          {t(
            "This run doesn't take messages — you can stop it.",
            "Dieser Lauf nimmt keine Nachrichten an – du kannst ihn stoppen.",
          )}
        </span>
      ),
      route: false,
    };
  }
  return {
    target: (
      <span className="inline-flex items-center gap-1">
        <CornerDownRight className="size-3.5" aria-hidden />
        {t(
          "To the running run · taken into account at the next step",
          "An den laufenden Lauf · wird beim nächsten Schritt berücksichtigt",
        )}
      </span>
    ),
    route: true,
  };
}

/** An inline error for a refused input (POST /runs/{id}/inputs) -- shown
 *  under the composer, never only as a toast. */
// eslint-disable-next-line react-refresh/only-export-components -- shared helper, used beside the component
export function runInputError(error: unknown, t: Translate): string {
  const status = error instanceof ApiError ? error.status : null;
  if (status === 409) {
    return error instanceof ApiError && /take messages/.test(error.message)
      ? t(
          "This run doesn't take messages — you can stop it.",
          "Dieser Lauf nimmt keine Nachrichten an – du kannst ihn stoppen.",
        )
      : t("The run has already finished.", "Der Lauf ist schon fertig.");
  }
  // The server's own sentence (e.g. the Assistant's secret refusal); the
  // length refusal and a validation list get the localized length hint.
  if (
    status === 422 &&
    error instanceof ApiError &&
    error.message &&
    !/^[[{]/.test(error.message) &&
    !/longer than|too long/i.test(error.message)
  ) {
    return error.message;
  }
  if (status === 422) {
    return t(
      "Your message is too long (at most 4,000 characters).",
      "Deine Nachricht ist zu lang (höchstens 4.000 Zeichen).",
    );
  }
  if (status === 429) {
    return t(
      "Too many messages are waiting for this run. Wait until it has read them.",
      "Für diesen Lauf warten zu viele Nachrichten. Warte, bis er sie gelesen hat.",
    );
  }
  return t(
    "Your message was not sent. Try again.",
    "Deine Nachricht wurde nicht gesendet. Versuch es noch einmal.",
  );
}

/** `consumedStep` of a message the run ended without reading: it became the
 *  chat's next turn instead (Ruling 59). Mirrors the backend's
 *  `CARRIED_OVER_STEP`. */
const CARRIED_OVER_STEP = -1;
/** `consumedStep` of a message the run never received: by the time it would
 *  have read it, the run was no longer one its author may steer (it now acts
 *  through someone's own account). Mirrors the backend's `REFUSED_STEP`. */
const REFUSED_STEP = -2;

/** `consumedStep` of a queued message its author withdrew: it is gone, never
 *  delivered. Mirrors the backend's `CANCELLED_STEP`. */
const CANCELLED_STEP = -3;

const queuedWaiting = (input: RunInputDTO) =>
  input.delivery === "next_turn" && input.consumedStep === null;

/** The messages written to a run, each with its fate. Only the run being
 *  steered asks the server (`enabled`); any other shows what is cached.
 *  Under the answer it lists what steered the run; `queued` (under the
 *  composer) lists instead the messages still waiting to become the next
 *  turn, each editable and cancellable (spec §5.5). */
export function RunInputs({
  runId,
  runState,
  enabled = true,
  queued = false,
}: {
  runId: string;
  runState: string;
  enabled?: boolean;
  queued?: boolean;
}) {
  const t = useT();
  const { data } = useRunSteering(enabled ? runId : null, runId);
  const all = data?.inputs ?? [];
  if (queued) return <QueuedInputs runId={runId} inputs={all.filter(queuedWaiting)} />;
  // A queued message becomes a turn of its own; a cancelled one is gone.
  const inputs = all.filter(
    (input) => input.delivery !== "next_turn" && input.consumedStep !== CANCELLED_STEP,
  );
  if (inputs.length === 0) return null;
  return (
    <ul className="space-y-1" aria-label={t("Messages to this run", "Nachrichten an diesen Lauf")}>
      {inputs.map((input) => (
        <li key={input.id} className="ml-auto max-w-[85%] rounded-lg bg-muted px-3 py-2 text-sm">
          <p className="whitespace-pre-wrap break-words">{input.text}</p>
          <p className="text-xs text-muted-foreground" aria-live="polite">
            {input.consumedStep === CARRIED_OVER_STEP
              ? t("Answered as a new message", "Wird als neue Nachricht beantwortet")
              : input.consumedStep === REFUSED_STEP
                ? t(
                    "Not delivered: this run now acts for someone else",
                    "Nicht zugestellt: Dieser Lauf handelt jetzt für jemand anderen",
                  )
                : input.consumedStep !== null
                  ? t(
                      `Taken into account in step ${input.consumedStep}`,
                      `Berücksichtigt in Schritt ${input.consumedStep}`,
                    )
                  : TERMINAL.has(runState)
                    ? t(
                        "The run finished before it read this",
                        "Der Lauf war fertig, bevor er das gelesen hat",
                      )
                    : t("Waiting for the next step", "Wartet auf den nächsten Schritt")}
          </p>
        </li>
      ))}
    </ul>
  );
}

const SMALL_BUTTON =
  "inline-flex h-11 items-center rounded-md px-2 text-xs font-medium text-muted-foreground hover:bg-background hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50 sm:h-7";

/** The messages queued behind the running answer, under the composer. */
function QueuedInputs({ runId, inputs }: { runId: string; inputs: RunInputDTO[] }) {
  const t = useT();
  const edit = useEditRunInput(runId);
  const cancel = useCancelRunInput(runId);
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (inputs.length === 0) return null;
  const refused = (e: unknown) =>
    setError(
      e instanceof ApiError && e.status === 409
        ? t("This message was already delivered.", "Diese Nachricht wurde schon zugestellt.")
        : runInputError(e, t),
    );
  const busy = edit.isPending || cancel.isPending;
  return (
    <div className="space-y-1">
      <ul className="space-y-1" aria-label={t("Queued messages", "Wartende Nachrichten")}>
        {inputs.map((input) => (
          <li key={input.id} className="ml-auto max-w-[85%] rounded-lg bg-muted px-3 py-2 text-sm">
            {editing?.id === input.id ? (
              <textarea
                aria-label={t("Edit queued message", "Wartende Nachricht bearbeiten")}
                value={editing.text}
                rows={2}
                onChange={(e) => setEditing({ id: input.id, text: e.target.value })}
                className="w-full resize-none rounded-md border border-border bg-background px-2 py-1 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
              />
            ) : (
              <p className="whitespace-pre-wrap break-words">{input.text}</p>
            )}
            <div className="flex flex-wrap items-center gap-1">
              <p className="mr-auto text-xs text-muted-foreground" aria-live="polite">
                {t("Sent when the answer is done", "Wird gesendet, wenn die Antwort fertig ist")}
              </p>
              {editing?.id === input.id ? (
                <>
                  <button
                    type="button"
                    className={SMALL_BUTTON}
                    disabled={busy || !editing.text.trim()}
                    onClick={() => {
                      setError(null);
                      edit.mutate(
                        { inputId: input.id, text: editing.text.trim() },
                        { onSuccess: () => setEditing(null), onError: refused },
                      );
                    }}
                  >
                    {t("Save", "Speichern")}
                  </button>
                  <button type="button" className={SMALL_BUTTON} onClick={() => setEditing(null)}>
                    {t("Discard", "Verwerfen")}
                  </button>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    className={SMALL_BUTTON}
                    disabled={busy}
                    onClick={() => {
                      setError(null);
                      setEditing({ id: input.id, text: input.text });
                    }}
                  >
                    {t("Edit", "Bearbeiten")}
                  </button>
                  <button
                    type="button"
                    className={SMALL_BUTTON}
                    disabled={busy}
                    onClick={() => {
                      setError(null);
                      cancel.mutate(input.id, { onError: refused });
                    }}
                  >
                    {t("Cancel", "Abbrechen")}
                  </button>
                </>
              )}
            </div>
          </li>
        ))}
      </ul>
      {error && (
        <p role="alert" className="px-1 text-xs text-status-error">
          {error}
        </p>
      )}
    </div>
  );
}
