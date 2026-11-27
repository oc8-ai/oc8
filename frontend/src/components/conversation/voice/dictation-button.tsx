// The composer's mic (spec 2026-11-27 §5.4): record, stop, and the words go
// into the message box for the person to read over. Nothing is sent. Shown
// only when the tenant has a dictation model and this browser can record.
import { useEffect, useRef, useState } from "react";
import { Mic, Square } from "lucide-react";
import { toast } from "sonner";

import { ApiError, transcribeChatAudio } from "@/lib/api";
import { useChatAudioCapabilities } from "@/lib/chat-audio";
import { useT } from "@/lib/i18n";
import { cn } from "@/lib/utils";

import {
  COUNTDOWN_FROM_MS,
  MAX_RECORDING_MS,
  MicrophoneDenied,
  formatClock,
  recordingSupported,
  useRecorder,
} from "./recorder";

/** The server's reason is worth showing for these; anything else is ours. */
const TOLD_AS_IS = new Set([409, 413, 429]);

export function DictationButton({
  sessionId,
  ensureSession,
  onTranscript,
  className,
}: {
  sessionId: string | null;
  /** A new chat has no session yet: made the way the first send makes it. */
  ensureSession: () => Promise<string>;
  /** The words, to put where the caret is. */
  onTranscript: (text: string) => void;
  className?: string;
}) {
  const t = useT();
  const capabilities = useChatAudioCapabilities();
  const [transcribing, setTranscribing] = useState(false);
  // Bumped when the chat changes: a transcript for the old chat is dropped.
  const generation = useRef(0);
  const recorder = useRecorder((recording) => void transcribe(recording));
  const { cancel } = recorder;

  const previous = useRef(sessionId);
  useEffect(() => {
    const before = previous.current;
    previous.current = sessionId;
    // The first session made under a dictation (null -> id) is the same chat.
    if (before !== null && before !== sessionId) {
      generation.current += 1;
      cancel();
    }
  }, [sessionId, cancel]);

  async function transcribe(recording: Blob) {
    const mine = generation.current;
    setTranscribing(true);
    try {
      const target = sessionId ?? (await ensureSession());
      const { text } = await transcribeChatAudio(target, recording);
      if (mine === generation.current && text.trim()) onTranscript(text.trim());
    } catch (error) {
      if (mine !== generation.current) return;
      toast.error(
        error instanceof ApiError && TOLD_AS_IS.has(error.status)
          ? error.message
          : t(
              "Your dictation could not be turned into text. Try again.",
              "Dein Diktat konnte nicht in Text umgewandelt werden. Versuch es noch einmal.",
            ),
      );
    } finally {
      setTranscribing(false);
    }
  }

  async function begin() {
    try {
      await recorder.start();
    } catch (error) {
      toast.error(
        error instanceof MicrophoneDenied
          ? t("Microphone access was denied.", "Der Mikrofonzugriff wurde verweigert.")
          : t("No microphone could be used.", "Es konnte kein Mikrofon verwendet werden."),
      );
    }
  }

  if (!capabilities.speechToText || !recordingSupported()) return null;
  const recording = recorder.phase === "recording";
  const elapsed = recorder.elapsedMs;
  const clock =
    elapsed >= COUNTDOWN_FROM_MS
      ? t(
          `${formatClock(MAX_RECORDING_MS - elapsed)} left`,
          `noch ${formatClock(MAX_RECORDING_MS - elapsed)}`,
        )
      : formatClock(elapsed);

  return (
    <>
      {recording && (
        <span
          className={cn(
            "px-1 font-mono text-xs tabular-nums",
            elapsed >= COUNTDOWN_FROM_MS ? "text-status-error" : "text-muted-foreground",
          )}
        >
          {clock}
        </span>
      )}
      <button
        type="button"
        onClick={() => (recording ? recorder.stop() : void begin())}
        disabled={transcribing || recorder.phase === "starting"}
        aria-pressed={recording}
        aria-label={
          recording
            ? t("Stop dictation", "Diktat beenden")
            : t("Dictate a message", "Nachricht diktieren")
        }
        title={
          recording
            ? t("Stop dictation", "Diktat beenden")
            : t("Dictate a message", "Nachricht diktieren")
        }
        className={cn(
          className,
          recording
            ? "bg-status-error/15 text-status-error"
            : "text-muted-foreground hover:bg-muted hover:text-foreground",
          "disabled:opacity-50",
        )}
      >
        {recording ? (
          <Square className="size-3.5" aria-hidden />
        ) : (
          <Mic className={cn("size-4", transcribing && "animate-pulse")} aria-hidden />
        )}
      </button>
      <span role="status" aria-live="polite" className="sr-only">
        {recording
          ? t("Recording", "Aufnahme läuft")
          : transcribing
            ? t("Turning your dictation into text …", "Dein Diktat wird in Text umgewandelt …")
            : ""}
      </span>
    </>
  );
}
