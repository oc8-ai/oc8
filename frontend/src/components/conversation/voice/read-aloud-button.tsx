// The speaker under an answer (spec 2026-11-27 §5.4): reads it aloud, and
// stops. Shown only when the tenant has a read-aloud model; a restricted
// answer needs a local one, otherwise the button says why it is off.
import { useEffect, useId } from "react";
import { Square, Volume2 } from "lucide-react";
import { toast } from "sonner";

import { fetchAnswerSpeech } from "@/lib/api";
import { speechRefusal, useChatAudioCapabilities } from "@/lib/chat-audio";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import { ActionButton } from "../action-button";
import { PlaybackBlocked, playSpeech, stopSpeech, useSpeechPhase } from "./speech-player";

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
  // Per view: the same answer open in the dock and in the full chat are two
  // readings, and closing one does not stop the other.
  const view = useId();
  const key = `${view}:${messageId}`;
  const phase = useSpeechPhase(key);
  const reasonId = useId();
  // Leaving the chat (or paging to another answer) stops this reading.
  useEffect(() => () => stopSpeech(key), [key]);

  if (!capabilities.textToSpeech) return null;
  const blocked = restricted !== false && !capabilities.textToSpeechLocal;
  const active = phase !== null;
  const reason = t(
    "Read aloud is not available for restricted answers with this speech model.",
    "Vorlesen ist für eingeschränkte Antworten mit diesem Sprachmodell nicht verfügbar.",
  );

  function toggle() {
    if (active) {
      stopSpeech(key);
      return;
    }
    // Called in the click itself: Safari plays only what a tap started.
    playSpeech(key, (signal) => fetchAnswerSpeech(sessionId, messageId, signal)).catch(
      (error: unknown) => {
        toast.error(
          error instanceof PlaybackBlocked
            ? t("Tap again to play.", "Tippe noch einmal zum Abspielen.")
            : (speechRefusal(error) ??
                t(
                  "The answer could not be read aloud. Try again.",
                  "Die Antwort konnte nicht vorgelesen werden. Versuch es noch einmal.",
                )),
        );
      },
    );
  }

  return (
    <>
      <ActionButton
        label={t("Read aloud", "Vorlesen")}
        onClick={toggle}
        pressed={active}
        disabled={blocked}
        describedBy={blocked ? reasonId : undefined}
        title={blocked ? reason : undefined}
      >
        {active ? (
          <Square className={cn("size-3.5", phase === "loading" && "animate-pulse")} aria-hidden />
        ) : (
          <Volume2 className="size-4" aria-hidden />
        )}
      </ActionButton>
      {blocked && (
        <span id={reasonId} className="sr-only">
          {reason}
        </span>
      )}
    </>
  );
}
