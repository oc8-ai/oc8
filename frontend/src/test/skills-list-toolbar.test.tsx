// Task 16 (Design System Consistency plan): the Skills list page now goes
// through <ListToolbar> (Task 14) instead of the old hand-rolled search box
// and category/origin filter chips -- server-side search/filter/group/page/
// archive state lives in a local `ListQueryState` and is threaded straight
// into `useSkills(params)` (Task 15).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const useSkillsMock = vi.fn();

vi.mock("@/lib/hooks", () => ({
  useSkills: (params: unknown) => useSkillsMock(params),
  useCreateSkill: () => ({ mutateAsync: vi.fn(), isPending: false }),
  useRestoreSkill: () => ({ mutate: vi.fn(), isPending: false }),
  useImportSkills: () => ({ mutate: vi.fn(), isPending: false }),
  usePreviewSkillImport: () => ({ mutate: vi.fn(), isPending: false, data: undefined }),
  useAgents: () => ({ data: { items: [], totalCount: 0 } }),
  useAssignSkill: () => ({ mutate: vi.fn(), isPending: false }),
  useDepartments: () => ({ data: { items: [], totalCount: 0 } }),
  useUpdateSkill: () => ({ mutate: vi.fn(), isPending: false }),
  usePublishSkillVersion: () => ({ mutate: vi.fn(), isPending: false }),
  useDuplicateSkill: () => ({ mutate: vi.fn(), isPending: false }),
}));

import { SkillsPage } from "@/routes/skills";

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SkillsPage />
    </QueryClientProvider>,
  );
}

describe("Skills list page", () => {
  beforeEach(() => {
    useSkillsMock.mockReset();
    useSkillsMock.mockReturnValue({ data: { items: [], totalCount: 0 } });
  });

  it("re-fetches with the typed search term", async () => {
    renderPage();

    fireEvent.change(screen.getByPlaceholderText(/search skills/i), {
      target: { value: "email" },
    });

    const lastCall = useSkillsMock.mock.calls.at(-1)?.[0];
    expect(lastCall).toMatchObject({ search: "email" });
  });

  it("shows the archived toggle and re-fetches with includeArchived when checked", async () => {
    renderPage();

    fireEvent.click(screen.getByText(/show archived/i));

    const lastCall = useSkillsMock.mock.calls.at(-1)?.[0];
    expect(lastCall).toMatchObject({ includeArchived: true });
  });

  it("re-reads the open skill off the refreshed page, so a saved rename shows in the sheet header", () => {
    // Task 22 live-E2E finding: the sheet took a snapshot of the row that was
    // clicked, so after Save the header still said the old name while the Name
    // input already held the new one. Simulating what the list query does after
    // the mutation invalidates it -- same id, new name -- must move the header.
    const before = {
      id: "s1",
      name: "Old name",
      description: "d",
      category: "operations",
      origin: "local",
      version: "0.1.0",
      author: "a@b.test",
      tools: [],
      knowledge: [],
      guardrails: [],
      instructions: "",
      usedByAgents: 0,
      installs: null,
      price: null,
      updatedAt: "",
      currentVersionId: "v1",
      deletedAt: null,
    };
    useSkillsMock.mockReturnValue({ data: { items: [before], totalCount: 1 } });
    const { rerender } = renderPage();

    fireEvent.click(screen.getByRole("button", { name: /open/i }));
    expect(screen.getByRole("heading", { name: "Old name" })).toBeTruthy();

    useSkillsMock.mockReturnValue({
      data: { items: [{ ...before, name: "New name" }], totalCount: 1 },
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    rerender(
      <QueryClientProvider client={qc}>
        <SkillsPage />
      </QueryClientProvider>,
    );

    expect(screen.getByRole("heading", { name: "New name" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "Old name" })).toBeNull();
  });
});
