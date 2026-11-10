import { useEffect, useMemo, useRef } from "react";

import { getToken } from "@/lib/api";
import { liveWsUrl } from "@/lib/live/ws-url";
import type { RealtimeEvent } from "@/lib/live/types";

const MAX_BACKOFF = 15_000;
/** The close code the backend uses for every refusal on this socket. */
const POLICY_VIOLATION = 1008;
const OPEN = 1;

export interface LiveSocket {
  /** Ask the server for this run's live events. Remembered, and sent again
   *  after every reconnect: a member-only socket gets a run's tokens only
   *  while it watches it (backend realtime/manager.py). Counted, so two
   *  views of the same run each hold it until both let go. */
  watch(runId: string): void;
  /** Local only: the server has no "unwatch" message, it drops a watch when
   *  the socket closes. Forgetting it here keeps it out of the next reconnect. */
  unwatch(runId: string): void;
}

function watchFrame(runId: string): string {
  return JSON.stringify({ type: "run.watch", runId });
}

export function useLiveConnection(
  onEvent: (e: RealtimeEvent) => void,
  onOpen: () => void,
  onClose?: () => void,
  onUnwatched?: (runId: string) => void,
): LiveSocket {
  const onEventRef = useRef(onEvent);
  const onOpenRef = useRef(onOpen);
  const onCloseRef = useRef(onClose);
  const onUnwatchedRef = useRef(onUnwatched);
  onEventRef.current = onEvent;
  onOpenRef.current = onOpen;
  onCloseRef.current = onClose;
  onUnwatchedRef.current = onUnwatched;
  const wsRef = useRef<WebSocket | null>(null);
  // runId -> how many callers watch it.
  const watched = useRef<Map<string, number>>(new Map());

  const socket = useMemo<LiveSocket>(
    () => ({
      watch(runId) {
        const count = watched.current.get(runId) ?? 0;
        watched.current.set(runId, count + 1);
        if (count > 0) return;
        const ws = wsRef.current;
        if (ws && ws.readyState === OPEN) ws.send(watchFrame(runId));
      },
      unwatch(runId) {
        const count = watched.current.get(runId) ?? 0;
        if (count <= 1) watched.current.delete(runId);
        else watched.current.set(runId, count - 1);
      },
    }),
    [],
  );

  useEffect(() => {
    let closed = false;
    let backoff = 1000;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (closed) return;
      // Get the token from the SAME canonical flow the API client uses
      // (in-memory promise + optional localStorage cache), so the socket
      // authenticates even when localStorage is unavailable or not yet
      // populated -- not by racing an independent localStorage read.
      void getToken()
        .then((token) => {
          if (closed) return;
          const ws = new WebSocket(liveWsUrl(token));
          wsRef.current = ws;
          ws.onopen = () => {
            backoff = 1000;
            // A fresh socket watches nothing on the server: ask again.
            for (const runId of watched.current.keys()) ws.send(watchFrame(runId));
            onOpenRef.current();
          };
          ws.onmessage = (ev) => {
            let frame: RealtimeEvent;
            try {
              frame = JSON.parse(ev.data as string) as RealtimeEvent;
            } catch {
              return; /* ignore malformed frame */
            }
            // Watch control frames are not events (no id, no data): answer
            // them here rather than hand them to the dedup/patcher pipeline.
            if (frame.type === "run.watching") return;
            if (frame.type === "run.unwatched") {
              // The member lost access to the run (manager.py's re-check).
              // Forget it so a reconnect does not ask again; the caller
              // refetches the run, which now answers with what he may see.
              const runId = (frame as unknown as { runId?: unknown }).runId;
              if (typeof runId === "string") {
                watched.current.delete(runId);
                onUnwatchedRef.current?.(runId);
              }
              return;
            }
            onEventRef.current(frame);
          };
          ws.onclose = (ev) => {
            if (wsRef.current === ws) wsRef.current = null;
            if (closed) return;
            onCloseRef.current?.();
            // 1008 is the server saying "not you": the socket carries the
            // activity feed and is gated on `run:view`, which an employee whose
            // authority is a seat does not hold. Retrying that once a second for
            // the length of his working day is a reconnect loop, so a refusal
            // goes straight to the ceiling. Not "never again": the same code is
            // sent for a token this connection could no longer verify, and a
            // session that silently stops receiving events until the tab is
            // reloaded is the worse of the two failures.
            if (ev.code === POLICY_VIOLATION) backoff = MAX_BACKOFF;
            timer = setTimeout(connect, backoff);
            backoff = Math.min(backoff * 2, MAX_BACKOFF);
          };
          ws.onerror = () => ws.close();
        })
        .catch(() => {
          if (closed) return;
          timer = setTimeout(connect, backoff);
          backoff = Math.min(backoff * 2, MAX_BACKOFF);
        });
    };

    connect();
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      wsRef.current?.close();
    };
  }, []);

  return socket;
}
