import { useQueryClient } from "@tanstack/react-query";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { applyEvent, liveQueryKeys } from "@/lib/live/apply-event";
import { useLiveConnection, type LiveSocket } from "@/lib/live/connection";
import { toastForEvent } from "@/lib/live/toast-for-event";
import type { RealtimeEvent } from "@/lib/live/types";

type LiveConnectionStatus = "connected" | "disconnected";

const LiveConnectionStatusContext = createContext<LiveConnectionStatus>("connected");

/** Whether the realtime socket is currently up. "disconnected" the moment a
 * close/error fires, "connected" again the moment it reopens -- distinct
 * from the reconnect backoff itself, which callers of this hook don't need
 * to know the timing of, only the current yes/no. */
// eslint-disable-next-line react-refresh/only-export-components -- shared helper, used beside the component
export function useLiveConnectionStatus(): LiveConnectionStatus {
  return useContext(LiveConnectionStatusContext);
}

const LiveSocketContext = createContext<LiveSocket | null>(null);

/** Watch these runs for as long as the caller is mounted. Ids that drop out of
 *  the list are unwatched. A no-op outside LiveUpdatesProvider (tests). */
// eslint-disable-next-line react-refresh/only-export-components -- shared helper, used beside the component
export function useWatchRuns(runIds: readonly string[]): void {
  const socket = useContext(LiveSocketContext);
  const key = runIds.join(",");
  useEffect(() => {
    if (!socket) return;
    const ids = key ? key.split(",") : [];
    for (const id of ids) socket.watch(id);
    return () => {
      for (const id of ids) socket.unwatch(id);
    };
  }, [socket, key]);
}

export function LiveUpdatesProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const seen = useRef<Set<string>>(new Set());
  const [status, setStatus] = useState<LiveConnectionStatus>("connected");

  const onEvent = useCallback(
    (e: RealtimeEvent) => {
      if (seen.current.has(e.id)) return;
      seen.current.add(e.id);
      if (seen.current.size > 500) {
        // bound the dedup set
        seen.current = new Set(Array.from(seen.current).slice(-250));
      }
      applyEvent(qc, e);
      toastForEvent(e);
    },
    [qc],
  );

  const onOpen = useCallback(() => {
    setStatus("connected");
    // reconnect-resync: refetch the live queries so any pub/sub gap is closed
    // Events may have been missed while the socket was down, so the last
    // state one announced is no longer worth more than what the server says.
    // Dropped before the refetch below, so it cannot override it.
    qc.removeQueries({ queryKey: ["run-live-state"] });
    qc.invalidateQueries({ queryKey: ["run"] });
    for (const key of liveQueryKeys) {
      qc.invalidateQueries({ queryKey: key });
    }
  }, [qc]);

  const onClose = useCallback(() => setStatus("disconnected"), []);

  // The server stopped this run's events (the member lost access). Refetch
  // it: the GET answers with what he may still see, and a view that keeps
  // waiting for tokens that will never come would look stuck.
  const onUnwatched = useCallback(
    (runId: string) => {
      qc.invalidateQueries({ queryKey: ["run", runId] });
    },
    [qc],
  );

  const socket = useLiveConnection(onEvent, onOpen, onClose, onUnwatched);
  return (
    <LiveSocketContext.Provider value={socket}>
      <LiveConnectionStatusContext.Provider value={status}>
        {children}
      </LiveConnectionStatusContext.Provider>
    </LiveSocketContext.Provider>
  );
}
