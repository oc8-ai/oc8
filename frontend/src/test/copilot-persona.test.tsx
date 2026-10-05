import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as hc from "@/lib/hooks-copilot";
import { CopilotPersonaHeader } from "@/components/copilot-persona";

vi.mock("@/lib/api", () => ({ api: { get: vi.fn(), post: vi.fn() } }));
afterEach(() => vi.restoreAllMocks());
const wrap = (ui: React.ReactElement) =>
  render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);

describe("CopilotPersonaHeader", () => {
  it("shows name and computed status", () => {
    vi.spyOn(hc, "useCopilotProfile").mockReturnValue({
      data: {
        displayName: "Alfred",
        avatar: { shape: "star", color: "teal" },
        pausedAt: null,
        status: "working",
        activeCount: 2,
        agentId: "a",
      },
    } as never);
    wrap(<CopilotPersonaHeader variant="page" />);
    expect(screen.getByText("Alfred")).toBeInTheDocument();
    expect(screen.getByText("Working on 2 things")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /pause/i })).toBeInTheDocument();
  });
  it("offers resume when paused", () => {
    vi.spyOn(hc, "useCopilotProfile").mockReturnValue({
      data: {
        displayName: "Copilot",
        avatar: { shape: "round", color: "indigo" },
        pausedAt: "x",
        status: "paused",
        activeCount: 0,
        agentId: "a",
      },
    } as never);
    wrap(<CopilotPersonaHeader variant="page" />);
    expect(screen.getByRole("button", { name: /resume/i })).toBeInTheDocument();
  });
  it("dock variant has no pause button and renders children", () => {
    vi.spyOn(hc, "useCopilotProfile").mockReturnValue({
      data: {
        displayName: "Copilot",
        avatar: { shape: "drop", color: "rose" },
        pausedAt: null,
        status: "ready",
        activeCount: 0,
        agentId: "a",
      },
    } as never);
    wrap(
      <CopilotPersonaHeader variant="dock">
        <span>child-slot</span>
      </CopilotPersonaHeader>,
    );
    expect(screen.queryByRole("button", { name: /pause/i })).not.toBeInTheDocument();
    expect(screen.getByText("child-slot")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
  });
});
