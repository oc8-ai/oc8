import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EditionBanners, editionPageTitle } from "@/components/app-shell";
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

describe("edition page titles", () => {
  const items = [
    {
      id: "page",
      to: "/edition-page",
      label: { en: "Edition page", de: "Edition-Seite" },
      icon: (() => null) as never,
    },
  ];
  const en = (a: string) => a;
  const de = (_a: string, b: string) => b;

  it("titles an edition page after its navigation item", () => {
    expect(editionPageTitle(items, "/edition-page", en)).toBe("Edition page");
    expect(editionPageTitle(items, "/edition-page/sub", de)).toBe("Edition-Seite");
  });

  it("has no title for other paths", () => {
    expect(editionPageTitle(items, "/edition-pages", en)).toBeUndefined();
    expect(editionPageTitle(items, "/settings", en)).toBeUndefined();
  });
});
