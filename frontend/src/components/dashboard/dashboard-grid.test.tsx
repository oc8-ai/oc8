import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, fireEvent } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { DashboardGrid } from "./dashboard-grid";
import type { WidgetInstance } from "@/lib/hooks";

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useBudgetStatus: () => ({ data: undefined, isPending: true }),
    useActivity: () => ({ data: [], isPending: false }),
    useRuns: () => ({ data: [], isPending: false }),
  };
});

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const widgets: WidgetInstance[] = [
  { id: "w1", type: "budget", x: 0, y: 0, w: 3, h: 3, config: {} },
  { id: "w2", type: "activity", x: 3, y: 0, w: 3, h: 4, config: {} },
];

describe("DashboardGrid", () => {
  it("renders one WidgetFrame per instance", () => {
    render(<DashboardGrid widgets={widgets} editing onChange={vi.fn()} />, { wrapper });
    expect(screen.getByText("Budget")).toBeInTheDocument();
    expect(screen.getByText("Activity")).toBeInTheDocument();
  });

  it("removing a widget calls onChange with it filtered out", () => {
    const onChange = vi.fn();
    render(<DashboardGrid widgets={widgets} editing onChange={onChange} />, { wrapper });
    const removeButtons = screen.getAllByRole("button", { name: /remove widget/i });
    fireEvent.click(removeButtons[0]);
    expect(onChange).toHaveBeenCalledWith([widgets[1]]);
  });

  it("has no resize handles unless editing", () => {
    // react-grid-layout keeps the handle in the DOM and hides it with CSS via
    // the item's `react-resizable-hide` class, so a visible handle is one whose
    // parent lacks that class.
    const visible = ".react-resizable-handle";
    const { container, rerender } = render(
      <DashboardGrid widgets={widgets} editing={false} onChange={vi.fn()} />,
      { wrapper },
    );
    expect(container.querySelector(`:not(.react-resizable-hide) > ${visible}`)).toBeNull();
    rerender(<DashboardGrid widgets={widgets} editing onChange={vi.fn()} />);
    expect(container.querySelector(`:not(.react-resizable-hide) > ${visible}`)).not.toBeNull();
  });
});
