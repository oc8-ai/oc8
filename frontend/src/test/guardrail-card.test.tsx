import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const m = vi.hoisted(() => ({ put: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, put: m.put, get: vi.fn(), post: vi.fn(), delete: vi.fn() },
  };
});

import { GuardrailCard } from "@/components/guardrail-card";
import { RUN_COMPONENT_REGISTRY } from "@/components/run-record-card";

const PROPS = {
  agentId: "a1",
  agentName: "Sales agent",
  toolKey: "gmail",
  level: "ask_first",
  baseLevel: "allowed",
};

function wrap(ui: React.ReactNode) {
  return render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  m.put.mockResolvedValue({ agentId: "a1", agentName: "Sales agent", summary: [], tools: [] });
});

describe("GuardrailCard", () => {
  it("shows the proposed rule as tool and level", () => {
    wrap(<GuardrailCard props={PROPS} />);
    expect(screen.getByText("Gmail → Ask me first")).toBeInTheDocument();
  });

  it("saves only on Confirm, with the member's own PUT, then says Active", async () => {
    wrap(<GuardrailCard props={PROPS} />);
    expect(m.put).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    await waitFor(() =>
      expect(m.put).toHaveBeenCalledWith("/agents/a1/guardrails/me/gmail", { level: "ask_first" }),
    );
    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });

  it("Not now dismisses without saving", () => {
    wrap(<GuardrailCard props={PROPS} />);
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    expect(m.put).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });

  it("says so in one sentence when the save fails", async () => {
    m.put.mockRejectedValue(new Error("boom"));
    wrap(<GuardrailCard props={PROPS} />);
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(await screen.findByText(/couldn't save this rule/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirm" })).toBeInTheDocument();
  });

  it("ignores a level it does not know", () => {
    wrap(<GuardrailCard props={{ ...PROPS, level: "allowed_everything" }} />);
    expect(screen.queryByRole("button", { name: "Confirm" })).toBeNull();
  });

  it("is registered in the run component registry", () => {
    expect(RUN_COMPONENT_REGISTRY.guardrail_card).toBe(GuardrailCard);
  });
});
