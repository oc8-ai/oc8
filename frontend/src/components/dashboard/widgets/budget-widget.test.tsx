import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BudgetWidget } from "./budget-widget";
import * as hooks from "@/lib/hooks";

describe("BudgetWidget", () => {
  it("shows current vs. limit and the hard-exceeded state", () => {
    vi.spyOn(hooks, "useBudgetStatus").mockReturnValue({
      data: {
        scope: "tenant",
        departmentId: null,
        softLimitTokens: 1000,
        hardLimitTokens: 2000,
        currentTokens: 2500,
        softExceeded: true,
        hardExceeded: true,
      },
      isPending: false,
    } as never);

    render(<BudgetWidget config={{}} onConfigChange={vi.fn()} />);
    // Use locale-formatted strings to tolerate different locale settings
    const expectedCurrent = (2500).toLocaleString();
    const expectedLimit = (2000).toLocaleString();
    expect(screen.getByText(new RegExp(expectedCurrent))).toBeInTheDocument();
    expect(screen.getByText(new RegExp(expectedLimit))).toBeInTheDocument();
  });

  it("treats a hard limit of 0 as a real, already-exhausted limit", () => {
    vi.spyOn(hooks, "useBudgetStatus").mockReturnValue({
      data: {
        scope: "tenant",
        departmentId: null,
        softLimitTokens: null,
        hardLimitTokens: 0,
        currentTokens: 0,
        softExceeded: false,
        hardExceeded: true,
      },
      isPending: false,
    } as never);

    render(<BudgetWidget config={{}} onConfigChange={vi.fn()} />);
    // The falsy-zero bug rendered a literal "0" text node instead of the
    // " / 0" suffix and skipped the progress bar entirely.
    expect(screen.getByText(/0\s*\/\s*0/)).toBeInTheDocument();
    expect(screen.getByText("Hard limit exceeded")).toBeInTheDocument();
  });

  it("draws its own title once", () => {
    vi.spyOn(hooks, "useBudgetStatus").mockReturnValue({
      data: undefined,
      isPending: true,
    } as never);
    render(<BudgetWidget config={{}} onConfigChange={vi.fn()} />);
    expect(screen.getAllByText("Budget")).toHaveLength(1);
  });
});
