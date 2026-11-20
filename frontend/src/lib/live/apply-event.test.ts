import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { applyEvent, liveQueryKeys } from "@/lib/live/apply-event";
import { notificationKeys } from "@/lib/notifications-api";
import type { RealtimeEvent } from "@/lib/live/types";

describe("notification.created", () => {
  it("invalidates the notification queries", () => {
    const qc = new QueryClient();
    qc.setQueryData(notificationKeys.unreadCount(), { count: 1 });
    qc.setQueryData(notificationKeys.list({}), { items: [] });
    applyEvent(qc, {
      id: "e1",
      type: "notification.created",
      data: { kind: "run.failed" },
    } as unknown as RealtimeEvent);
    expect(qc.getQueryState(notificationKeys.unreadCount())?.isInvalidated).toBe(true);
    expect(qc.getQueryState(notificationKeys.list({}))?.isInvalidated).toBe(true);
  });

  it("is resynced on reconnect", () => {
    expect(liveQueryKeys).toContainEqual(notificationKeys.all);
  });
});
