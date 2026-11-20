import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";

import { toastForEvent } from "@/lib/live/toast-for-event";
import type { RealtimeEvent } from "@/lib/live/types";

vi.mock("sonner", () => {
  const fn = Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
  });
  return { toast: fn };
});

let seq = 0;
function runEnded(data: Record<string, unknown>): RealtimeEvent {
  seq += 1;
  return { id: `e${seq}`, type: "run.status", data: { run_id: "r", ...data } } as never;
}

afterEach(() => {
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe("toastForEvent and finished runs (E2E D)", () => {
  it("stays quiet for a chat's run, whoever's it is", () => {
    toastForEvent(runEnded({ agent_id: "a1", state: "done", source: "chat" }));
    toastForEvent(runEnded({ agent_id: "a1", state: "failed", source: "chat" }));
    expect(toast.success).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("toasts a background run in the reader's language", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-09T10:00:00Z"));
    const de = (en: string, german?: string) => german ?? en;
    toastForEvent(runEnded({ agent_id: "a2", state: "done", source: "cron" }), de);
    expect(toast.success).toHaveBeenCalledWith("Agent-Run abgeschlossen", {
      description: "Ein Hintergrund-Agent hat seinen Lauf beendet.",
    });
    vi.setSystemTime(new Date("2026-11-09T10:01:00Z"));
    toastForEvent(runEnded({ agent_id: "a2", state: "done", source: "cron" }));
    expect(toast.success).toHaveBeenLastCalledWith("Agent run finished", {
      description: "A background agent finished its run.",
    });
  });
});
