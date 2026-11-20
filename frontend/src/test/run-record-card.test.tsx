import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RecordCard, RUN_COMPONENT_REGISTRY, RunComponents } from "@/components/run-record-card";

describe("RecordCard", () => {
  it("renders a title, subtitle, fields, and link", () => {
    render(
      <RecordCard
        props={{
          title: "Acme GmbH — 12.400 €",
          subtitle: "Verhandlung",
          fields: [{ label: "Abschluss erwartet", value: "2026-09-01" }],
          link_label: "In CRM öffnen",
          link_url: "https://crm.example.com/deals/42",
        }}
      />,
    );
    expect(screen.getByText("Acme GmbH — 12.400 €")).toBeInTheDocument();
    expect(screen.getByText("Verhandlung")).toBeInTheDocument();
    expect(screen.getByText("Abschluss erwartet")).toBeInTheDocument();
    expect(screen.getByText("2026-09-01")).toBeInTheDocument();
    const link = screen.getByRole("link", { name: "In CRM öffnen" });
    expect(link).toHaveAttribute("href", "https://crm.example.com/deals/42");
  });

  it("renders without a subtitle, fields, or link", () => {
    render(<RecordCard props={{ title: "Just a title" }} />);
    expect(screen.getByText("Just a title")).toBeInTheDocument();
  });

  it("is registered under record_card", () => {
    expect(RUN_COMPONENT_REGISTRY.record_card).toBe(RecordCard);
  });

  it("refuses to render a javascript: URI as a link", () => {
    render(
      <RecordCard
        props={{ title: "T", link_label: "Click me", link_url: "javascript:alert(1)" }}
      />,
    );
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});

describe("RunComponents", () => {
  it("renders known components and nothing for unknown or prototype keys", () => {
    const { container } = render(
      <RunComponents
        components={[
          { componentKey: "record_card", props: { title: "Known card" } },
          { componentKey: "nope", props: {} },
          { componentKey: "toString", props: {} },
        ]}
      />,
    );
    expect(screen.getByText("Known card")).toBeInTheDocument();
    expect(container.firstElementChild?.children).toHaveLength(1);
  });

  it("renders nothing for no components", () => {
    const { container } = render(<RunComponents components={[]} />);
    expect(container).toBeEmptyDOMElement();
  });
});
