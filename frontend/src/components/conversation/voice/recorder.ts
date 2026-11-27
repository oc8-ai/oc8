// One dictation recording (spec 2026-11-27 §5.4): the microphone through
// MediaRecorder, at most 10 minutes. Recorded at 16 kbit/s because the
// server's daily allowance counts the upload's bytes at that rate.
import { useCallback, useEffect, useRef, useState } from "react";

export const MAX_RECORDING_MS = 10 * 60_000;
/** From here on the clock shows what is left instead of what has passed. */
export const COUNTDOWN_FROM_MS = 9 * 60_000 + 30_000;
export const RECORDING_BITS_PER_SECOND = 16_000;
/** In order of preference; the server takes webm, ogg and mp4. */
const RECORDING_TYPES = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"];

/** The microphone was refused (by the person, or by the page's policy). */
export class MicrophoneDenied extends Error {}

export function recordingSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.MediaRecorder !== "undefined" &&
    typeof navigator.mediaDevices?.getUserMedia === "function"
  );
}

/** The first type this browser records; undefined lets it choose. */
export function pickRecordingType(): string | undefined {
  const supported = (type: string) =>
    typeof MediaRecorder.isTypeSupported === "function" && MediaRecorder.isTypeSupported(type);
  return RECORDING_TYPES.find(supported);
}

/** 65 000 ms -> "1:05". */
export function formatClock(ms: number): string {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

interface Take {
  recorder: MediaRecorder;
  stream: MediaStream;
  ticker: ReturnType<typeof setInterval>;
  cap: ReturnType<typeof setTimeout>;
  /** Thrown away when it stops (chat switch, unmount), not handed on. */
  discard: boolean;
}

export type RecorderPhase = "idle" | "starting" | "recording";

/** `onRecorded` gets the recording once it stops (by `stop()` or the cap). */
export function useRecorder(onRecorded: (recording: Blob) => void) {
  const [phase, setPhase] = useState<RecorderPhase>("idle");
  const [elapsedMs, setElapsedMs] = useState(0);
  const take = useRef<Take | null>(null);
  // A cancel while the microphone is still being asked for.
  const cancelled = useRef(false);
  const handOn = useRef(onRecorded);
  useEffect(() => {
    handOn.current = onRecorded;
  });

  const stop = useCallback(() => {
    const current = take.current;
    if (current && current.recorder.state !== "inactive") current.recorder.stop();
  }, []);

  /** Stops without handing the recording on. */
  const cancel = useCallback(() => {
    cancelled.current = true;
    if (take.current) take.current.discard = true;
    stop();
  }, [stop]);

  /** Throws `MicrophoneDenied`, or whatever else kept it from starting. */
  const start = useCallback(async () => {
    if (take.current) return;
    cancelled.current = false;
    setPhase("starting");
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (error) {
      setPhase("idle");
      const name = (error as { name?: string } | null)?.name;
      if (name === "NotAllowedError" || name === "SecurityError") throw new MicrophoneDenied();
      throw error;
    }
    if (cancelled.current) {
      stream.getTracks().forEach((track) => track.stop());
      setPhase("idle");
      return;
    }
    const mimeType = pickRecordingType();
    let recorder: MediaRecorder;
    try {
      recorder = new MediaRecorder(stream, {
        ...(mimeType ? { mimeType } : {}),
        audioBitsPerSecond: RECORDING_BITS_PER_SECOND,
      });
    } catch (error) {
      stream.getTracks().forEach((track) => track.stop());
      setPhase("idle");
      throw error;
    }
    const chunks: Blob[] = [];
    const startedAt = Date.now();
    const current: Take = {
      recorder,
      stream,
      discard: false,
      ticker: setInterval(() => setElapsedMs(Date.now() - startedAt), 250),
      cap: setTimeout(() => {
        if (recorder.state !== "inactive") recorder.stop();
      }, MAX_RECORDING_MS),
    };
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0) chunks.push(event.data);
    };
    recorder.onstop = () => {
      clearInterval(current.ticker);
      clearTimeout(current.cap);
      current.stream.getTracks().forEach((track) => track.stop());
      if (take.current === current) take.current = null;
      setPhase("idle");
      setElapsedMs(0);
      if (current.discard || chunks.length === 0) return;
      const type = recorder.mimeType || mimeType || chunks[0].type;
      handOn.current(new Blob(chunks, { type }));
    };
    take.current = current;
    setElapsedMs(0);
    recorder.start();
    setPhase("recording");
  }, []);

  // Leaving the page drops a recording in progress and frees the microphone.
  useEffect(() => cancel, [cancel]);

  return { phase, elapsedMs, start, stop, cancel };
}
