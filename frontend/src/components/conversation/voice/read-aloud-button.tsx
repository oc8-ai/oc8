// The speaker under an answer (spec 2026-11-27 §5.4): reads it aloud, and
// stops. Shown only when the tenant has a read-aloud model; a restricted
// answer needs a local one, otherwise the button says why it is off.
import { useEffect, useId } from "react";
import { Square, Volume2 } from "lucide-react";
import { toast } from "sonner";

import { ApiError, fetchAnswerSpeech } from "@/lib/api";
import { useChatAudioCapabilities } from "@/lib/chat-audio";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { playSpeech, stopSpeech, useSpeechPhase } from "./speech-player";

/** The server's reason is worth showing for these; anything else is ours. */
const TOLD_AS_IS = new Set([409, 413, 429]);

export function ReadAloudButton({
  sessionId,
  messageId,
  restricted,
}: {
  sessionId: string;
  messageId: string;
  /** Only `false` counts as unrestricted: an older answer may lack the flag. */
  restricted: boolean | undefined;
}) {
  const t = useT();
  const capabilities = useChatAudioCapabilities();
  const phase = useSpeechPhase(messageId);
  const reasonId = useId();
  // Leaving the chat (or paging to another answer) stops this reading.
  useEffect(() => () => stopSpeech(messageId), [messageId]);

  if (!capabilities.textToSpeech) return null;
  const blocked = restricted !== false && !capabilities.textToSpeechLocal;
  const active = phase !== null;

  function toggle() {
    if (active) {
      stopSpeech(messageId);
      return;
    }
    playSpeech(messageId, (signal) => fetchAnswerSpeech(sessionId, messageId, signal)).catch(
      (error: unknown) => {
        toast.error(
          error instanceof ApiError && TOLD_AS_IS.has(error.status)
            ? error.message
            : t(
                "The answer could not be read aloud. Try again.",
                "Die Antwort konnte nicht vorgelesen werden. Versuch es noch einmal.",
              ),
        );
      },
    );
  }

  return (
    <>
      <button
        type="button"
        onClick={toggle}
        disabled={blocked}
        aria-pressed={active}
        aria-label={active ? t("Stop reading", "Vorlesen stoppen") : t("Read aloud", "Vorlesen")}
        aria-describedby={blocked ? reasonId : undefined}
        title={
          blocked
            ? t(
                "Read aloud is not available for restricted answers with this speech model.",
                "Vorlesen ist für eingeschränkte Antworten mit diesem Sprachmodell nicht verfügbar.",
              )
            : undefined
        }
        className={cn(
          "grid size-11 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent sm:size-9",
          active && "text-foreground",
        )}
      >
        {active ? (
          <Square className={cn("size-3.5", phase === "loading" && "animate-pulse")} aria-hidden />
        ) : (
          <Volume2 className="size-4" aria-hidden />
        )}
      </button>
      {blocked && (
        <span id={reasonId} className="sr-only">
          {t(
            "Read aloud is not available for restricted answers with this speech model.",
            "Vorlesen ist für eingeschränkte Antworten mit diesem Sprachmodell nicht verfügbar.",
          )}
        </span>
      )}
    </>
  );
}
