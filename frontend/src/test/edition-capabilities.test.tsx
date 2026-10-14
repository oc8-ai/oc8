import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

let caps: string[] | null = null;
vi.mock("@/lib/governance-hooks", () => ({
  useCallerCapabilities: () => caps,
  useCan: () => () => true,
  useMay: () => () => true,
  useAssignedRoleName: () => null,
}));

import { EditionCapabilityGate } from "@/components/app-shell";
import {
  EditionCompositionProvider,
  composeFrontendEdition,
  isPathBlocked,
} from "@/edition/composition";

const extension = {
  id: "gated",
  capabilities: ["edition.feature.a"],
  navigation: [
    {
      id: "feature",
      to: "/edition/feature",
      label: { en: "Feature", de: "Funktion" },
      icon: (() => null) as never,
    },
  ],
};
const { gates } = composeFrontendEdition([extension]);

describe("edition capability gates", () => {
  it("blocks a path when the capabilities lack one the extension declares", () => {
    expect(isPathBlocked(gates, "/edition/feature", [])).toBe(true);
    expect(isPathBlocked(gates, "/edition/feature/sub", ["other"])).toBe(true);
  });

  it("allows a path when the capabilities include them", () => {
    expect(isPathBlocked(gates, "/edition/feature", ["edition.feature.a"])).toBe(false);
  });

  it("changes nothing when the capabilities are unknown", () => {
    expect(isPathBlocked(gates, "/edition/feature", null)).toBe(false);
  });

  it("does not gate other paths", () => {
    expect(isPathBlocked(gates, "/edition/features", [])).toBe(false);
    expect(isPathBlocked(gates, "/settings", [])).toBe(false);
  });
});

describe("edition capability panel", () => {
  const renderGate = (path: string) =>
    render(
      <EditionCompositionProvider extensions={[extension]}>
        <EditionCapabilityGate path={path}>
          <div>page</div>
        </EditionCapabilityGate>
      </EditionCompositionProvider>,
    );

  it("shows the neutral panel instead of the page when blocked", () => {
    caps = [];
    renderGate("/edition/feature");
    expect(
      screen.getByText("This feature is not available for your organisation."),
    ).toBeInTheDocument();
    expect(screen.queryByText("page")).not.toBeInTheDocument();
  });

  it("shows the page when the capabilities include the declared one", () => {
    caps = ["edition.feature.a"];
    renderGate("/edition/feature");
    expect(screen.getByText("page")).toBeInTheDocument();
  });

  it("shows the page when the capabilities are unknown", () => {
    caps = null;
    renderGate("/edition/feature");
    expect(screen.getByText("page")).toBeInTheDocument();
  });
});

describe("edition navigation", () => {
  it("drops the items of a gated extension the caller lacks, using the same rule as the routes", async () => {
    const { readFileSync } = await import("node:fs");
    const { join } = await import("node:path");
    const src = readFileSync(join(process.cwd(), "src/components/app-shell.tsx"), "utf-8");
    expect(src).toContain("lacksCapabilities(g, callerCapabilities)");
    expect(src).toContain(".filter((item) => !hiddenEditionPaths.has(item.to))");
  });
});
