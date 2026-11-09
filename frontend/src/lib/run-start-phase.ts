import { useEffect, useState } from "react";

/** The phases an isolated run passes through before its first step
 *  (backend oc8.runtime.start_phase): the sandbox is being placed, then the
 *  container runs and its shell has not asked for a step yet. An in-process
 *  run never has one. */
const START_PHASES = new Set(["provisioning", "starting"]);

export function isStartPhase(phase: string | null | undefined): boolean {
  return !!phase && START_PHASES.has(phase);
}

/** After this long the line explains why: a cold start (a new node, an image
 *  pull) takes minutes. */
export const SLOW_START_MS = 20_000;

export const START_TEXT = {
  en: "Starting the workspace …",
  de: "Arbeitsumgebung wird gestartet …",
} as const;

export const SLOW_START_TEXT = {
  en: "After a quiet spell the first run can take up to three minutes.",
  de: "Das kann beim ersten Lauf nach einer Pause bis zu drei Minuten dauern.",
} as const;

/** True once `active` has held for SLOW_START_MS; false again when it ends. */
export function useSlowStart(active: boolean): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    setSlow(false);
    if (!active) return;
    const timer = window.setTimeout(() => setSlow(true), SLOW_START_MS);
    return () => window.clearTimeout(timer);
  }, [active]);
  return active && slow;
}
