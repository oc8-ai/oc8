import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";

import { applyEvent } from "@/lib/live/apply-event";

function event(type: string, data: Record<string, unknown>) {
  return { id: crypto.randomUUID(), type, tenantid: "t", time: "", source: "", data };
}

describe("chat live events", () => {
  it("chat.message_created invalidates that session's messages and detail", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(
      qc,
      event("chat.message_created", {
        session_id: "s1",
        message_id: "m1",
        role: "assistant",
        run_id: "r1",
      }),
    );
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(["chat", "messages", "s1"]));
    expect(keys).toContain(JSON.stringify(["chat", "session", "s1"]));
    expect(keys).toContain(JSON.stringify(["chat", "sessions"]));
  });

  it("chat.session_updated refetches the chat lists and that chat, not its transcript", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, event("chat.session_updated", { session_id: "s1" }));
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(["chat", "sessions"]));
    expect(keys).toContain(JSON.stringify(["chat", "session", "s1"]));
    expect(keys).not.toContain(JSON.stringify(["chat", "messages", "s1"]));
  });

  it("a terminal run.status refreshes chat transcripts", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, event("run.status", { run_id: "r1", state: "done" }));
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(["chat", "messages"]));
    expect(keys).toContain(JSON.stringify(["chat", "session"]));
  });

  it("a terminal run.status refetches the run, so withheld deltas come back from the persisted run", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, event("run.status", { run_id: "r1", state: "failed" }));
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(["run", "r1"]));
  });

  it("a non-terminal run.status leaves chat and the run alone", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, event("run.status", { run_id: "r1", state: "running" }));
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).not.toContain(JSON.stringify(["chat", "messages"]));
    expect(keys).not.toContain(JSON.stringify(["run", "r1"]));
  });

  it("copilot.agent_builder refetches that proposal's card", () => {
    const qc = new QueryClient();
    const spy = vi.spyOn(qc, "invalidateQueries");
    applyEvent(qc, event("copilot.agent_builder", { proposal_id: "p1", session_id: "s1" }));
    const keys = spy.mock.calls.map((c) => JSON.stringify(c[0]?.queryKey));
    expect(keys).toContain(JSON.stringify(["copilot", "builder", "p1"]));
  });
});
