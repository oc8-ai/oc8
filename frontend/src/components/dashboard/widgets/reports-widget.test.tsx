import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ReportsWidget } from "./reports-widget";
import * as hooks from "@/lib/hooks";

function wrapper({ children }: { children: ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("ReportsWidget", () => {
  it("renders the existing ReportsSection inside a scrollable tile", () => {
    vi.spyOn(hooks, "useReports").mockReturnValue({ data: [], isPending: false } as never);
    render(<ReportsWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getByTestId("reports-widget-scroll")).toBeInTheDocument();
  });

  it("draws its own title once", () => {
    vi.spyOn(hooks, "useReports").mockReturnValue({ data: [], isPending: false } as never);
    render(<ReportsWidget config={{}} onConfigChange={vi.fn()} />, { wrapper });
    expect(screen.getAllByText("Reports")).toHaveLength(1);
  });
});
