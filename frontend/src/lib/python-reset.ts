// Takes the page's Run Python sandbox away (the frame, its worker and every
// file it holds) when what it ran for ends: another chat, the chat closed,
// signing out. The runner registers itself here when it starts, so callers
// need not load it (it is fetched on the first Run only).

let reset: (() => void) | null = null;

/** Called by the runner with what tears its sandbox down. */
export function onPythonReset(fn: () => void): void {
  reset = fn;
}

/** Stops any run and removes the sandbox; the next Run starts over. */
export function resetPython(): void {
  reset?.();
}
