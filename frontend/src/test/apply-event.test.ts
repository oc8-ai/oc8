import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { applyEvent, mcpTestLogKey } from "@/lib/live/apply-event";
import type { RealtimeEvent } from "@/lib/live/types";

function event(type: string, data: Record<string, unknown>): RealtimeEvent {
  return { id: "e1", type, tenantid: "t1", time: "2026-08-21T00:00:00Z", source: "test", data };
}

describe("run.component_rendered", () => {
  it("appends a rendered component to the run's cache entry", () => {
    const qc = new QueryClient();
    qc.setQueryData(["run", "r1"], { id: "r1", state: "running", steps: 1, toolCalls: [] });

    applyEvent(
      qc,
      event("run.component_rendered", {
        run_id: "r1",
        component_key: "record_card",
        props: { title: "Acme" },
      }),
    );

    const run = qc.getQueryData(["run", "r1"]) as { components?: Array<Record<string, unknown>> };
    expect(run.components).toEqual([{ componentKey: "record_card", props: { title: "Acme" } }]);
  });

  it("appends rather than replaces on a second event", () => {
    const qc = new QueryClient();
    qc.setQueryData(["run", "r1"], {
      id: "r1",
      state: "running",
      steps: 1,
      toolCalls: [],
      components: [{ componentKey: "record_card", props: { title: "First" } }],
    });

    applyEvent(
      qc,
      event("run.component_rendered", {
        run_id: "r1",
        component_key: "record_card",
        props: { title: "Second" },
      }),
    );

    const run = qc.getQueryData(["run", "r1"]) as { components?: Array<Record<string, unknown>> };
    expect(run.components).toHaveLength(2);
  });

  it("does nothing for a run that is not in the cache", () => {
    const qc = new QueryClient();
    applyEvent(
      qc,
      event("run.component_rendered", {
        run_id: "unknown",
        component_key: "record_card",
        props: {},
      }),
    );
    expect(qc.getQueryData(["run", "unknown"])).toBeUndefined();
  });
});

describe("run.todos_updated", () => {
  it("replaces the run's todos wholesale", () => {
    const qc = new QueryClient();
    qc.setQueryData(["run", "r1"], {
      id: "r1",
      state: "running",
      steps: 1,
      toolCalls: [],
      todos: [{ content: "First", status: "pending" }],
    });

    applyEvent(
      qc,
      event("run.todos_updated", {
        run_id: "r1",
        todos: [
          { content: "First", status: "completed" },
          { content: "Second", status: "in_progress" },
        ],
      }),
    );

    const run = qc.getQueryData(["run", "r1"]) as { todos?: Array<Record<string, unknown>> };
    expect(run.todos).toEqual([
      { content: "First", status: "completed" },
      { content: "Second", status: "in_progress" },
    ]);
  });

  it("does nothing for a run that is not in the cache", () => {
    const qc = new QueryClient();
    applyEvent(
      qc,
      event("run.todos_updated", {
        run_id: "unknown",
        todos: [{ content: "First", status: "pending" }],
      }),
    );
    expect(qc.getQueryData(["run", "unknown"])).toBeUndefined();
  });
});

describe("run.tool_call", () => {
  it("appends one tool call to the run's toolCalls array", () => {
    const qc = new QueryClient();
    qc.setQueryData(["run", "r1"], { id: "r1", state: "running", steps: 1, toolCalls: [] });

    applyEvent(
      qc,
      event("run.tool_call", {
        run_id: "r1",
        call: { tool: "search_record", arguments: { model: "res.partner" }, result: "3 found" },
      }),
    );

    const run = qc.getQueryData(["run", "r1"]) as { toolCalls?: Array<Record<string, unknown>> };
    expect(run.toolCalls).toEqual([
      { tool: "search_record", arguments: { model: "res.partner" }, result: "3 found" },
    ]);
  });

  it("appends rather than replaces on a second event", () => {
    const qc = new QueryClient();
    qc.setQueryData(["run", "r1"], {
      id: "r1",
      state: "running",
      steps: 1,
      toolCalls: [{ tool: "search_record", arguments: {}, result: "first" }],
    });

    applyEvent(
      qc,
      event("run.tool_call", {
        run_id: "r1",
        call: { tool: "get_record", arguments: {}, result: "second" },
      }),
    );

    const run = qc.getQueryData(["run", "r1"]) as { toolCalls?: Array<Record<string, unknown>> };
    expect(run.toolCalls).toHaveLength(2);
  });

  it("does nothing for a run that is not in the cache", () => {
    const qc = new QueryClient();
    applyEvent(
      qc,
      event("run.tool_call", { run_id: "unknown", call: { tool: "x", arguments: {}, result: "" } }),
    );
    expect(qc.getQueryData(["run", "unknown"])).toBeUndefined();
  });
});

describe("run.token_delta", () => {
  it("starts the live answer on the first fragment", () => {
    const qc = new QueryClient();
    qc.setQueryData(["run", "r1"], { id: "r1", state: "running", steps: 1, toolCalls: [] });

    applyEvent(qc, event("run.token_delta", { run_id: "r1", text: "Hal" }));

    const run = qc.getQueryData(["run", "r1"]) as { liveAnswer?: string };
    expect(run.liveAnswer).toBe("Hal");
  });

  it("concatenates rather than replacing on a second event", () => {
    const qc = new QueryClient();
    qc.setQueryData(["run", "r1"], {
      id: "r1",
      state: "running",
      steps: 1,
      toolCalls: [],
      liveAnswer: "Hal",
    });

    applyEvent(qc, event("run.token_delta", { run_id: "r1", text: "lo" }));

    const run = qc.getQueryData(["run", "r1"]) as { liveAnswer?: string };
    expect(run.liveAnswer).toBe("Hallo");
  });

  it("does nothing for a run that is not in the cache", () => {
    const qc = new QueryClient();
    applyEvent(qc, event("run.token_delta", { run_id: "unknown", text: "x" }));
    expect(qc.getQueryData(["run", "unknown"])).toBeUndefined();
  });
});

describe("mcp.test.log", () => {
  it("appends the first line even with no prior cache entry", () => {
    const qc = new QueryClient();

    applyEvent(
      qc,
      event("mcp.test.log", { connection_id: "c1", step: "spawn", message: "Starting session…" }),
    );

    expect(qc.getQueryData(mcpTestLogKey("c1"))).toEqual([
      { step: "spawn", message: "Starting session…" },
    ]);
  });

  it("appends rather than replaces on a second event", () => {
    const qc = new QueryClient();
    qc.setQueryData(mcpTestLogKey("c1"), [{ step: "spawn", message: "Starting session…" }]);

    applyEvent(
      qc,
      event("mcp.test.log", {
        connection_id: "c1",
        step: "handshake",
        message: "Handshake complete.",
      }),
    );

    expect(qc.getQueryData(mcpTestLogKey("c1"))).toEqual([
      { step: "spawn", message: "Starting session…" },
      { step: "handshake", message: "Handshake complete." },
    ]);
  });

  it("keeps a second connection's log separate", () => {
    const qc = new QueryClient();
    qc.setQueryData(mcpTestLogKey("c1"), [{ step: "spawn", message: "Starting session…" }]);

    applyEvent(
      qc,
      event("mcp.test.log", { connection_id: "c2", step: "spawn", message: "Starting session…" }),
    );

    expect(qc.getQueryData(mcpTestLogKey("c1"))).toHaveLength(1);
    expect(qc.getQueryData(mcpTestLogKey("c2"))).toHaveLength(1);
  });
});
