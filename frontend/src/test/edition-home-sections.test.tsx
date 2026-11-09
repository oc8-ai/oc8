import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EditionCompositionProvider, composeFrontendEdition } from "@/edition/composition";
import { EditionHomeSections } from "@/edition/slots";

describe("edition home sections", () => {
  it("collects them in extension order and refuses a duplicate id", () => {
    const A = () => <p>A</p>;
    const B = () => <p>B</p>;
    const composed = composeFrontendEdition([
      { id: "one", homeSections: [{ id: "a", component: A }] },
      { id: "two", homeSections: [{ id: "b", component: B }] },
    ]);
    expect(composed.homeSections.map((s) => s.id)).toEqual(["a", "b"]);
    expect(() =>
      composeFrontendEdition([
        { id: "one", homeSections: [{ id: "a", component: A }] },
        { id: "two", homeSections: [{ id: "a", component: B }] },
      ]),
    ).toThrow("Duplicate edition home section: a");
    render(
      <>
        {composed.homeSections.map(({ id, component: C }) => (
          <C key={id} />
        ))}
      </>,
    );
    expect(screen.getByText("A")).toBeInTheDocument();
  });

  it("renders each edition's cards through the home slot", () => {
    const Card = () => <p>Connect your apps</p>;
    render(
      <EditionCompositionProvider
        extensions={[{ id: "one", homeSections: [{ id: "c", component: Card }] }]}
      >
        <EditionHomeSections />
      </EditionCompositionProvider>,
    );
    expect(screen.getByText("Connect your apps")).toBeInTheDocument();
  });
});
