import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EditionBanners } from "@/components/app-shell";
import { EditionCompositionProvider } from "@/edition/composition";

describe("edition banners", () => {
  it("renders every banner an edition contributes, in order", () => {
    render(
      <EditionCompositionProvider
        extensions={[
          { id: "first", banners: [{ id: "one", component: () => <div role="status">First notice</div> }] },
          { id: "second", banners: [{ id: "two", component: () => <div role="status">Second notice</div> }] },
        ]}
      >
        <EditionBanners />
      </EditionCompositionProvider>,
    );
    const notices = screen.getAllByRole("status");
    expect(notices.map((n) => n.textContent)).toEqual(["First notice", "Second notice"]);
  });

  it("renders nothing without an edition", () => {
    const { container } = render(<EditionBanners />);
    expect(container).toBeEmptyDOMElement();
  });
});
