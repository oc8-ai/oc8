// Which artifact is open beside the conversation, shared by the cards in the
// answers (they open it, and keep a live one current while it streams) and
// the panel. Provided by the conversation; without it, answers show their
// blocks inline as before.

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";

import type { AssistantTurnModel, TurnModel } from "../types";
import { artifactsOnPath, versionsOf, type Artifact } from "./extract";

export type ArtifactsState = {
  /** The open artifact's id, or null. */
  openId: string | null;
  /** Open `artifact` in the panel (or bring it to the front). */
  open: (artifact: Artifact) => void;
  /** A newer state of the open artifact while its answer streams. */
  sync: (artifact: Artifact) => void;
};

export const ArtifactsContext = createContext<ArtifactsState | null>(null);

export function useArtifacts(): ArtifactsState | null {
  return useContext(ArtifactsContext);
}

/** Under this width the panel is a sheet over the chat. */
const SHEET_QUERY = "(max-width: 1023px)";

function subscribeSheet(onChange: () => void): () => void {
  if (typeof window.matchMedia !== "function") return () => undefined;
  const mql = window.matchMedia(SHEET_QUERY);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
}

function belowPanelWidth(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(SHEET_QUERY).matches;
}

/** Whether the viewport is too narrow for the panel beside the chat. */
export function usePanelAsSheet(): boolean {
  return useSyncExternalStore(subscribeSheet, belowPanelWidth, () => false);
}

/** The id a live answer's artifacts are numbered under, until it is saved. */
export function liveArtifactsId(runId: string): string {
  return `run:${runId}`;
}

/** The answer an assistant turn shows by default: the variant on the visible
 *  branch, else the newest. */
function shownVariant(turn: AssistantTurnModel) {
  return (
    (turn.selectedId && turn.variants.find((v) => v.id === turn.selectedId)) ||
    turn.variants[turn.variants.length - 1]
  );
}

function sameArtifact(a: Artifact, b: Artifact): boolean {
  return a.id === b.id && a.source === b.source && a.closed === b.closed && a.title === b.title;
}

/** The conversation's panel state: what is open, its versions on the
 *  current message path, and the context the cards read. */
export function useArtifactsPanel(sessionId: string | null, turns: TurnModel[]) {
  const [picked, setPicked] = useState<Artifact | null>(null);
  // Another chat starts with the panel closed.
  const [seenSession, setSeenSession] = useState(sessionId);
  if (seenSession !== sessionId) {
    setSeenSession(sessionId);
    setPicked(null);
  }
  const path = useMemo(
    () =>
      turns.flatMap((turn) => {
        if (turn.kind !== "assistant") return [];
        const shown = shownVariant(turn);
        return shown ? [shown] : [];
      }),
    [turns],
  );
  const onPath = useMemo(() => artifactsOnPath(path), [path]);
  // The picked artifact as the path has it now; a live one, once its answer
  // is saved, is the saved answer's artifact at the same place.
  let current = picked;
  if (picked) {
    const landed = path.find((m) => m.runId && liveArtifactsId(m.runId) === picked.messageId);
    const id = landed ? `${landed.id}:${picked.id.slice(picked.messageId.length + 1)}` : picked.id;
    current = onPath.find((a) => a.id === id) ?? picked;
  }
  const versions = current ? versionsOf(current, onPath) : [];
  const open = useCallback((artifact: Artifact) => setPicked(artifact), []);
  const sync = useCallback(
    (artifact: Artifact) =>
      setPicked((prev) =>
        prev && prev.id === artifact.id && !sameArtifact(prev, artifact) ? artifact : prev,
      ),
    [],
  );
  const close = useCallback(() => setPicked(null), []);
  const openId = current?.id ?? null;
  const context = useMemo<ArtifactsState>(() => ({ openId, open, sync }), [openId, open, sync]);
  return { current, versions, context, open, close };
}
