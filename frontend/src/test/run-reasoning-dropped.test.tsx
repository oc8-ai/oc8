import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunModelSteps } from "@/components/run-reasoning";
import { modelSteps } from "@/lib/run-model-steps";

describe("a step answered without its reasoning level", () => {
  it("is read off stepTimings, even with no usage recorded", () => {
    const [step] = modelSteps([{ step: 1, stepWallMs: 10, reasoningDropped: true }]);
    expect(step.reasoningDropped).toBe(true);
    expect(modelSteps([{ step: 1, tokensOut: 5, maxTokens: 10 }])[0]).not.toHaveProperty(
      "reasoningDropped",
    );
  });

  it("says so under the step", () => {
    render(
      <RunModelSteps
        live={false}
        stepTimings={[{ step: 1, tokensOut: 40, maxTokens: 4096, reasoningDropped: true }]}
      />,
    );
    expect(screen.getByTestId("reasoning-dropped")).toHaveTextContent(
      "Reasoning level not supported by this model, answered without thinking.",
    );
  });
});
