// E2E F15: the agent config page's knowledge panel stayed English in German
// ("Knowledge", "Read-only", and the sensitivity badges). Isolated in its own
// file so the always-German translator mock cannot leak elsewhere.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { KnowledgeBase } from "@/lib/mock-data";

vi.mock("@/lib/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/i18n")>();
  return { ...actual, useT: () => (_en: string, de: string) => de };
});
vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return { ...actual, useCreateGrant: () => ({ mutate: vi.fn(), isPending: false }) };
});

import { KnowledgeAssignment } from "@/components/knowledge-assignment";

const base = (id: string, sensitivity: KnowledgeBase["sensitivity"]) =>
  ({ id, name: id, sensitivity, docs: 3, roles: [], localOnly: false }) as unknown as KnowledgeBase;

describe("KnowledgeAssignment in German", () => {
  it("translates its title, read-only hint and sensitivity badges", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <KnowledgeAssignment
          mode="agent"
          readOnly
          bases={[base("a", "internal"), base("b", "confidential"), base("c", "restricted")]}
          enabled={[]}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByText("Wissen")).toBeInTheDocument();
    expect(screen.getByText("Nur lesbar")).toBeInTheDocument();
    expect(screen.getByText("Intern")).toBeInTheDocument();
    expect(screen.getByText("Vertraulich")).toBeInTheDocument();
    expect(screen.getByText("Streng vertraulich")).toBeInTheDocument();
    for (const english of ["Knowledge", "Read-only", "Internal", "Confidential", "Restricted"]) {
      expect(screen.queryByText(english)).not.toBeInTheDocument();
    }
  });
});
