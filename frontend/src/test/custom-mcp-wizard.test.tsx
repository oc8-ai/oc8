import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CustomMcpWizard } from "@/components/custom-mcp-wizard";

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient();
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

describe("CustomMcpWizard", () => {
  it("renders step 1 with the three server-type cards when open", () => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    expect(screen.getByText(/Local MCP server/i)).toBeInTheDocument();
    expect(screen.getByText(/Remote MCP server/i)).toBeInTheDocument();
    expect(screen.getByText(/Plain HTTP API/i)).toBeInTheDocument();
  });

  it("does not render anything when closed", () => {
    render(<CustomMcpWizard open={false} onOpenChange={() => {}} />, { wrapper });
    expect(screen.queryByText(/Local MCP server/i)).not.toBeInTheDocument();
  });

  it("blocks advancing from step 1 until a name and server type are chosen", () => {
    render(<CustomMcpWizard open onOpenChange={() => {}} />, { wrapper });
    const next = screen.getByRole("button", { name: /next/i });
    expect(next).toBeDisabled();
  });
});
