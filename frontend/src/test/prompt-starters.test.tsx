import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { PromptStarters } from "@/components/conversation/prompt-starters";

describe("PromptStarters", () => {
  it("sends a starter's prompt when it has one, else its title", () => {
    const pick = vi.fn();
    render(
      <PromptStarters
        starters={[
          {
            title: "Rechnungsprüfer",
            subtitle: "Prüft offene Rechnungen",
            prompt: "/agent Leg mir einen an.",
          },
          { title: "Was wartet auf mich?", subtitle: "" },
        ]}
        onPick={pick}
      />,
    );
    fireEvent.click(screen.getByText("Rechnungsprüfer"));
    fireEvent.click(screen.getByText("Was wartet auf mich?"));
    expect(pick.mock.calls).toEqual([["/agent Leg mir einen an."], ["Was wartet auf mich?"]]);
  });
});
