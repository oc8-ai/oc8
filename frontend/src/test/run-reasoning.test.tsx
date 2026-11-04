import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ReasoningBlock, RunFailureReason, RunModelSteps } from "@/components/run-reasoning";
import { modelSteps } from "@/lib/run-model-steps";

describe("modelSteps", () => {
  it("reads usage, budget and reasoning off each step", () => {
    const steps = modelSteps([
      {
        step: 1,
        stepWallMs: 900,
        tokensIn: 812,
        tokensOut: 4096,
        reasoningTokens: 4000,
        maxTokens: 4096,
        reasoning: "thinking",
      },
      { step: 2, stepWallMs: 300, tokensIn: 900, tokensOut: 40, maxTokens: 4096 },
    ]);
    expect(steps).toEqual([
      {
        step: 1,
        tokensIn: 812,
        tokensOut: 4096,
        reasoningTokens: 4000,
        maxTokens: 4096,
        reasoning: "thinking",
        budgetUsedUp: true,
      },
      {
        step: 2,
        tokensIn: 900,
        tokensOut: 40,
        reasoningTokens: null,
        maxTokens: 4096,
        reasoning: null,
        budgetUsedUp: false,
      },
    ]);
  });

  it("leaves out records written before usage was recorded", () => {
    expect(modelSteps([{ step: 1, stepWallMs: 900 }])).toEqual([]);
    expect(modelSteps(undefined)).toEqual([]);
  });
});

describe("ReasoningBlock", () => {
  it("is collapsed until opened", () => {
    render(<ReasoningBlock text="I should look up the order first." />);
    expect(screen.getByText("Thought")).toBeInTheDocument();
    expect(screen.queryByText("I should look up the order first.")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Thought/ }));
    expect(screen.getByText("I should look up the order first.")).toBeInTheDocument();
  });

  it("says it is still thinking while live", () => {
    render(<ReasoningBlock text="hmm" live defaultOpen />);
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
    expect(screen.getByText("hmm")).toBeInTheDocument();
  });
});

describe("RunModelSteps", () => {
  it("shows each step's usage against its budget", () => {
    render(
      <RunModelSteps
        live={false}
        stepTimings={[
          { step: 1, tokensIn: 812, tokensOut: 4096, reasoningTokens: 4000, maxTokens: 4096 },
        ]}
      />,
    );
    expect(screen.getByText("Step 1")).toBeInTheDocument();
    expect(
      screen.getByText("812 in · 4,096 out (4,000 reasoning) · budget 4,096"),
    ).toBeInTheDocument();
  });

  it("shows the live reasoning of the step in progress", () => {
    render(
      <RunModelSteps live liveReasoning={{ step: 2, text: "checking stock" }} stepTimings={[]} />,
    );
    expect(screen.getByText("Thinking…")).toBeInTheDocument();
    expect(screen.getByText("checking stock")).toBeInTheDocument();
  });

  it("renders nothing for a run without any of it", () => {
    const { container } = render(<RunModelSteps live={false} stepTimings={[{ step: 1 }]} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("RunFailureReason", () => {
  it("shows the reason", () => {
    render(<RunFailureReason reason="The model used its whole answer budget (4,096 tokens)." />);
    expect(screen.getByRole("alert")).toHaveTextContent("This run failed.");
    expect(screen.getByText(/whole answer budget/)).toBeInTheDocument();
  });

  it("says so when no reason was recorded", () => {
    render(<RunFailureReason reason={null} />);
    expect(screen.getByText("No reason was recorded.")).toBeInTheDocument();
  });
});
