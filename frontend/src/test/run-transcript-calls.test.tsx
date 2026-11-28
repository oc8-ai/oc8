// A run's tool calls showed `JSON.stringify(call)` for every call whose result
// was not a string: the row read `input`, which no runtime writes, instead of
// `arguments`, which both do. A person could not see what the agent asked for.
// The run also never said what it used in total.
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunTokenTotal, ToolCallRow } from "@/routes/agents.$id";

describe("ToolCallRow", () => {
  it("shows what the call asked for and what came back", () => {
    render(
      <ToolCallRow
        call={{
          tool: "odoo_search",
          arguments: { model: "calendar.event", limit: 80 },
          result: "12 records",
          state: "done",
          step: 3,
        }}
      />,
    );
    expect(screen.getByText("odoo_search")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();
    expect(screen.getByText(/model: "calendar\.event", limit: 80/)).toBeInTheDocument();
    expect(screen.getByText(/12 records/)).toBeInTheDocument();
    expect(screen.queryByText(/"step"/)).toBeNull();
  });

  it("opens to the full arguments and result", () => {
    const long = "x".repeat(400);
    render(<ToolCallRow call={{ tool: "t", arguments: { q: long }, result: long }} />);
    fireEvent.click(screen.getByRole("button", { name: /t/ }));
    expect(screen.getByText(new RegExp(`"q": "${long}"`))).toBeInTheDocument();
  });
});

describe("RunTokenTotal", () => {
  it("adds up every step and says each step resends the conversation", () => {
    render(
      <RunTokenTotal
        stepTimings={[
          { step: 1, tokensIn: 20000, tokensOut: 500 },
          { step: 2, tokensIn: 21000, tokensOut: 300 },
          { step: 3 },
        ]}
      />,
    );
    expect(screen.getByText(/41,000 in · 800 out/)).toBeInTheDocument();
    expect(screen.getByTitle(/whole conversation again/i)).toBeInTheDocument();
  });

  it("shows nothing when no step recorded its usage", () => {
    const { container } = render(<RunTokenTotal stepTimings={[{ step: 1 }]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
