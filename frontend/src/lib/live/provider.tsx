import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from "react";

import { applyEvent, liveQueryKeys } from "@/lib/live/apply-event";
import { useLiveConnection } from "@/lib/live/connection";
import { toastForEvent } from "@/lib/live/toast-for-event";
import type { RealtimeEvent } from "@/lib/live/types";

type LiveConnectionStatus = "connected" | "disconnected";

const LiveConnectionStatusContext = createContext<LiveConnectionStatus>("connected");

/** Whether the realtime socket is currently up. "disconnected" the moment a
 * close/error fires, "connected" again the moment it reopens -- distinct
 * from the reconnect backoff itself, which callers of this hook don't need
 * to know the timing of, only the current yes/no. */
export function useLiveConnectionStatus(): LiveConnectionStatus {
  return useContext(LiveConnectionStatusContext);
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

  useLiveConnection(onEvent, onOpen, onClose);
  return (
    <LiveConnectionStatusContext.Provider value={status}>
      {children}
    </LiveConnectionStatusContext.Provider>
  );
}
