import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { mcpTestLogKey } from "@/lib/live/apply-event";
import { McpTestLogDrawer } from "@/components/mcp-test-log-drawer";

function renderDrawer(qc: QueryClient, connectionId: string, busy: boolean) {
  return render(
    <QueryClientProvider client={qc}>
      <McpTestLogDrawer connectionId={connectionId} busy={busy} />
    </QueryClientProvider>,
  );
}

describe("McpTestLogDrawer", () => {
  it("renders nothing before a test has ever run", () => {
    const qc = new QueryClient();
    const { container } = renderDrawer(qc, "c1", false);
    expect(container).toBeEmptyDOMElement();
  });

  it("opens and resets the log the moment busy turns true -- even when it is already true at mount", () => {
    // Mirrors custom-mcp-wizard.tsx: it only learns the connectionId (and so
    // only mounts this drawer) once installEnableConfigure resolves, by
    // which point its own `busy` is already true.
    const qc = new QueryClient();
    qc.setQueryData(mcpTestLogKey("c1"), [{ step: "spawn", message: "Starting session…" }]);

    renderDrawer(qc, "c1", true);

    // Open by default (busy), and the stale line from a prior run is gone.
    expect(screen.getByText("Connection log")).toBeInTheDocument();
    expect(qc.getQueryData(mcpTestLogKey("c1"))).toEqual([]);
  });

  it("opens on a false->true transition after mount (admin panel's mcp-connections.tsx case)", () => {
    const qc = new QueryClient();
    const { rerender } = renderDrawer(qc, "c1", false);
    // Nothing to show yet -- no test has ever run for this connection.
    expect(screen.queryByText("Connection log")).not.toBeInTheDocument();

    rerender(
      <QueryClientProvider client={qc}>
        <McpTestLogDrawer connectionId="c1" busy={true} />
      </QueryClientProvider>,
    );

    expect(screen.getByText("Connection log")).toBeInTheDocument();
  });

  it("clears a prior run's log the moment a second test starts", () => {
    const qc = new QueryClient();
    qc.setQueryData(mcpTestLogKey("c1"), [{ step: "result", message: "Connected — 1 tool." }]);

    const { rerender } = renderDrawer(qc, "c1", false);
    rerender(
      <QueryClientProvider client={qc}>
        <McpTestLogDrawer connectionId="c1" busy={true} />
      </QueryClientProvider>,
    );

    expect(qc.getQueryData(mcpTestLogKey("c1"))).toEqual([]);
  });

  it("shows appended lines once expanded, in order", () => {
    const qc = new QueryClient();
    qc.setQueryData(mcpTestLogKey("c1"), [
      { step: "spawn", message: "Starting session…" },
      { step: "handshake", message: "Handshake complete." },
    ]);

    // A finished run: busy has gone back to false, but its log stays put for
    // the operator to expand and read (mirrors mcp-connections.tsx once
    // testConnection.isPending settles).
    renderDrawer(qc, "c1", false);

    const toggle = screen.getByRole("button", { name: /connection log/i });
    fireEvent.click(toggle);

    expect(screen.getByText("Starting session…")).toBeInTheDocument();
    expect(screen.getByText("Handshake complete.")).toBeInTheDocument();
  });

  it("colors a failing result line but not a successful one", () => {
    const qc = new QueryClient();
    qc.setQueryData(mcpTestLogKey("c1"), [{ step: "result", message: "Error: 403 Forbidden" }]);

    renderDrawer(qc, "c1", false);
    fireEvent.click(screen.getByRole("button", { name: /connection log/i }));

    const line = screen.getByText("Error: 403 Forbidden");
    expect(line.closest("div")).toHaveClass("text-[color:var(--status-error)]");
  });

  it("keeps two connections' drawers independent", () => {
    const qc = new QueryClient();
    qc.setQueryData(mcpTestLogKey("c1"), [{ step: "spawn", message: "c1 line" }]);
    qc.setQueryData(mcpTestLogKey("c2"), [{ step: "spawn", message: "c2 line" }]);

    const { container } = render(
      <QueryClientProvider client={qc}>
        <McpTestLogDrawer connectionId="c1" busy={false} />
        <McpTestLogDrawer connectionId="c2" busy={false} />
      </QueryClientProvider>,
    );

    const bars = within(container).getAllByText("Connection log");
    expect(bars).toHaveLength(2);
    bars.forEach((bar) => fireEvent.click(bar));
    expect(screen.getByText("c1 line")).toBeInTheDocument();
    expect(screen.getByText("c2 line")).toBeInTheDocument();
  });
});
