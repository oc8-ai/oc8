// Writing to a running run from the chat (spec §3.2): the composer's target
// line while a visible run is active, the inputs written to a run with what
// became of each, and the words for a refused input.
import type { ReactNode } from "react";
import { CornerDownRight } from "lucide-react";

import { ApiError } from "@/lib/api";
import { useRunSteering, type RunSteeringDTO } from "@/lib/conversation-api";
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

/** The messages written to a run, each with its fate. */
export function RunInputs({ runId, runState }: { runId: string; runState: string }) {
  const t = useT();
  const { data } = useRunSteering(runId);
  const inputs = data?.inputs ?? [];
  if (inputs.length === 0) return null;
  return (
    <ul className="space-y-1" aria-label={t("Messages to this run", "Nachrichten an diesen Lauf")}>
      {inputs.map((input) => (
        <li key={input.id} className="ml-auto max-w-[85%] rounded-lg bg-muted px-3 py-2 text-sm">
          <p className="whitespace-pre-wrap break-words">{input.text}</p>
          <p className="text-xs text-muted-foreground" aria-live="polite">
            {input.consumedStep !== null
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
