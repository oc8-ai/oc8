import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { WidgetTitle } from "./widget-title";

describe("WidgetTitle", () => {
  it("renders the title and the actions node", () => {
    render(<WidgetTitle title="Budget" actions={<button type="button">Act</button>} />);
    expect(screen.getByText("Budget")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Act" })).toBeInTheDocument();
  });

  it("has no border on its root", () => {
    const { container } = render(<WidgetTitle title="Budget" />);
    expect(container.firstElementChild?.className).not.toMatch(/\bborder\b/);
  });
});
