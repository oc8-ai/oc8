// The composer's mic (spec 2026-11-27 §5.4): record, stop, and the words go
// into the message box for the person to read over. Nothing is sent. Shown
// only when the tenant has a dictation model and this browser can record.
import { useEffect, useRef, useState } from "react";
import { Mic, Square } from "lucide-react";
import { toast } from "sonner";

import { transcribeChatAudio } from "@/lib/api";
import { MAX_DICTATION_BYTES, speechRefusal, useChatAudioCapabilities } from "@/lib/chat-audio";
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

export function DictationButton({
  sessionId,
  ensureSession,
  onTranscript,
  className,
}: {
  sessionId: string | null;
  /** A new chat has no session yet: made the way the first send makes it,
   *  and only once the recording has to be uploaded. */
  ensureSession: () => Promise<string>;
  /** The words, to put where the caret is. */
  onTranscript: (text: string) => void;
  className?: string;
}) {
  const t = useT();
  const capabilities = useChatAudioCapabilities();
  const [transcribing, setTranscribing] = useState(false);
  // Bumped when the chat changes: a recording or transcript for the old chat
  // is dropped.
  const generation = useRef(0);
  // While `ensureSession` runs for this dictation: the one session change
  // that is not a chat switch is the chat it creates.
  const creating = useRef(false);
  // The session it created, which the page may switch to only afterwards.
  const created = useRef<string | null>(null);
  const recorder = useRecorder((recording) => void transcribe(recording));
  const { cancel } = recorder;

  const previous = useRef(sessionId);
  useEffect(() => {
    const before = previous.current;
    previous.current = sessionId;
    if (before === sessionId) return;
    if (before === null && (creating.current || sessionId === created.current)) return;
    // Any other change is another chat: stop, and keep nothing for it.
    generation.current += 1;
    cancel();
  }, [sessionId, cancel]);

  async function transcribe(recording: Blob) {
    const mine = generation.current;
    if (recording.size > MAX_DICTATION_BYTES) {
      toast.error(
        t(
          "This recording is larger than 12 MB. Try a shorter one.",
          "Diese Aufnahme ist größer als 12 MB. Versuch eine kürzere.",
        ),
      );
      return;
    }
    setTranscribing(true);
    try {
      let target = sessionId;
      if (target === null) {
        creating.current = true;
        try {
          target = await ensureSession();
          created.current = target;
        } finally {
          creating.current = false;
        }
        // Switched to another chat while this one was being made.
        const shown = previous.current;
        if (mine !== generation.current || (shown !== null && shown !== target)) return;
      }
      const { text } = await transcribeChatAudio(target, recording);
      if (mine !== generation.current) return;
      if (text.trim()) onTranscript(text.trim());
      else toast.error(t("No speech was recognized.", "Es wurde keine Sprache erkannt."));
    } catch (error) {
      if (mine !== generation.current) return;
      toast.error(
        speechRefusal(error) ??
          t(
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
  const ending = recording && elapsed >= COUNTDOWN_FROM_MS;
  const clock = ending
    ? t(
        `${formatClock(MAX_RECORDING_MS - elapsed)} left`,
        `noch ${formatClock(MAX_RECORDING_MS - elapsed)}`,
      )
    : formatClock(elapsed);
  const label = t("Dictate a message", "Nachricht diktieren");

  return (
    <>
      {recording && (
        <span
          aria-hidden
          className={cn(
            "px-1 font-mono text-xs tabular-nums",
            ending ? "text-status-error" : "text-muted-foreground",
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
        aria-label={label}
        title={label}
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
      {/* Said once per state, not every second. */}
      <span role="status" aria-live="polite" className="sr-only">
        {ending
          ? t("Recording. It stops in 30 seconds.", "Aufnahme läuft. Sie endet in 30 Sekunden.")
          : recording
            ? t("Recording", "Aufnahme läuft")
            : transcribing
              ? t("Turning your dictation into text …", "Dein Diktat wird in Text umgewandelt …")
              : ""}
      </span>
    </>
  );
}
