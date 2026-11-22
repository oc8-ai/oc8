import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

// Task 11: SkillDrawer used to be a hand-rolled `fixed inset-0 ... flex
// justify-end` div; it now goes through the shared <DetailSheet> (Task 10),
// which renders Radix Sheet content with role="dialog". The amendment adds
// inline-editable name/description/category fields + a Save button that
// calls useUpdateSkill().

const useUpdateSkillMock = vi.fn();
const publishMock = vi.fn();
const duplicateMock = vi.fn();
const resetMock = vi.fn();

vi.mock("@/lib/hooks", () => ({
  useAgents: () => ({ data: [] }),
  useAssignSkill: () => ({ mutate: vi.fn(), isPending: false }),
  useUpdateSkill: () => useUpdateSkillMock(),
  usePublishSkillVersion: () => ({
    mutate: (vars: unknown, opts?: { onSuccess?: (r: unknown) => void }) => {
      publishMock(vars);
      opts?.onSuccess?.({
        version: "1.0.1",
        assignmentsUpdated: 1,
        assignmentsSkipped: 1,
        assignmentsNotAllowed: 1,
      });
    },
    isPending: false,
  }),
  useResetSkill: () => ({
    mutate: (vars: unknown, opts?: { onSuccess?: (r: unknown) => void }) => {
      resetMock(vars);
      opts?.onSuccess?.({ ...skill, version: "1.0.0", customizedAt: null });
    },
    isPending: false,
  }),
  useDuplicateSkill: () => ({
    mutate: (vars: unknown, opts?: { onSuccess?: (r: unknown) => void }) => {
      duplicateMock(vars);
      opts?.onSuccess?.({ ...skill, id: "sk-2", name: "Invoice Review (copy)" });
    },
    isPending: false,
  }),
}));

import type { Skill } from "@/lib/skills";
import { SkillDrawer } from "@/routes/skills";

const skill: Skill = {
  id: "sk-1",
  name: "Invoice Review",
  description: "Validates invoices against POs.",
  category: "finance",
  origin: "local",
  version: "1.0.0",
  author: "oc8 core",
  tools: ["Accounting"],
  guardrails: ["Amounts above €10 000 require human approval"],
  instructions: "Extract line items and book them.",
  currentVersionId: "v-1",
  usedByAgents: 2,
  assignmentCount: 2,
  updatedAt: "2026-08-01",
};

function renderDrawer(
  onClose: () => void = vi.fn(),
  s: Skill = skill,
  onDuplicated: (s: Skill) => void = vi.fn(),
) {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <SkillDrawer
        skill={s}
        categoryLabel="Finance"
        onClose={onClose}
        onDuplicated={onDuplicated}
      />
    </QueryClientProvider>,
  );
}

describe("SkillDrawer", () => {
  it("renders inside a DetailSheet (right-side sheet chrome)", () => {
    useUpdateSkillMock.mockReturnValue({ mutate: vi.fn(), isPending: false });

    renderDrawer();

    // A Radix Sheet's content renders with role="dialog" via SheetContent;
    // asserting that role is what proves this now goes through DetailSheet
    // rather than the old hand-rolled `fixed inset-0 ... flex justify-end` div.
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("edits the name field and calls useUpdateSkill() when Save is clicked", () => {
    const mutate = vi.fn();
    useUpdateSkillMock.mockReturnValue({ mutate, isPending: false });

    renderDrawer();

    const nameInput = screen.getByLabelText("Name");
    fireEvent.change(nameInput, { target: { value: "Invoice Review v2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(mutate).toHaveBeenCalledTimes(1);
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "sk-1",
        name: "Invoice Review v2",
        description: skill.description,
        category: skill.category,
      }),
      expect.anything(),
    );
  });

  it("publishes a new version when the instructions change, with the checkbox value", () => {
    const mutate = vi.fn();
    useUpdateSkillMock.mockReturnValue({ mutate, isPending: false });
    publishMock.mockClear();

    renderDrawer();

    fireEvent.change(screen.getByLabelText("Instructions"), {
      target: { value: "Extract line items, then book them." },
    });
    fireEvent.change(screen.getByLabelText("Tools (comma-separated)"), {
      target: { value: "Accounting, OCR" },
    });
    const box = screen.getByLabelText("Also update the 2 agents that use this skill");
    expect(box).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(publishMock).toHaveBeenCalledWith({
      skillId: "sk-1",
      instructions: "Extract line items, then book them.",
      tools: ["Accounting", "OCR"],
      knowledge: [],
      guardrails: skill.guardrails,
      updateAssignments: true,
    });
    // Nothing but content changed, so there is no meta PATCH.
    expect(mutate).not.toHaveBeenCalled();
  });

  it("sends updateAssignments=false when the checkbox is cleared, then PATCHes changed meta", () => {
    const mutate = vi.fn();
    useUpdateSkillMock.mockReturnValue({ mutate, isPending: false });
    publishMock.mockClear();

    renderDrawer();

    fireEvent.change(screen.getByLabelText("Guardrails (one per line)"), {
      target: { value: "Rule one\nRule two" },
    });
    fireEvent.click(screen.getByLabelText("Also update the 2 agents that use this skill"));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Invoice Review 2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(publishMock).toHaveBeenCalledWith(
      expect.objectContaining({ guardrails: ["Rule one", "Rule two"], updateAssignments: false }),
    );
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({ id: "sk-1", name: "Invoice Review 2" }),
      expect.anything(),
    );
  });

  it("does not offer the checkbox when no agent uses the skill", () => {
    useUpdateSkillMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
    renderDrawer(vi.fn(), { ...skill, assignmentCount: 0 });
    fireEvent.change(screen.getByLabelText("Instructions"), { target: { value: "New." } });
    expect(screen.queryByLabelText(/Also update the/)).not.toBeInTheDocument();
  });

  it("keeps a meta-only edit on PATCH without publishing a version", () => {
    const mutate = vi.fn();
    useUpdateSkillMock.mockReturnValue({ mutate, isPending: false });
    publishMock.mockClear();

    renderDrawer();
    fireEvent.change(screen.getByLabelText("Description"), { target: { value: "New text." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(publishMock).not.toHaveBeenCalled();
    expect(mutate).toHaveBeenCalledTimes(1);
  });

  it("edits a shipped (store) skill in place and still offers Duplicate", () => {
    useUpdateSkillMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
    publishMock.mockClear();

    renderDrawer(vi.fn(), { ...skill, origin: "store" });

    expect(screen.queryByText("Duplicate this skill to change it")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Duplicate" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Instructions"), { target: { value: "Ours." } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(publishMock).toHaveBeenCalledWith(
      expect.objectContaining({ skillId: "sk-1", instructions: "Ours." }),
    );
    // Not customised yet: nothing to reset.
    expect(screen.queryByText("Customised")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Reset to shipped version" }),
    ).not.toBeInTheDocument();
  });

  it("badges a customised shipped skill and resets it only after confirming", async () => {
    useUpdateSkillMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
    resetMock.mockClear();

    renderDrawer(vi.fn(), { ...skill, origin: "store", customizedAt: "2026-11-20T10:00:00Z" });

    expect(screen.getByText("Customised")).toBeInTheDocument();
    expect(screen.queryByText(/A newer shipped version is available/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reset to shipped version" }));
    expect(resetMock).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole("button", { name: "Reset" }));
    await waitFor(() =>
      expect(resetMock).toHaveBeenCalledWith(
        expect.objectContaining({ id: "sk-1", updateAssignments: true }),
      ),
    );
  });

  it("does not reset when the confirmation is cancelled", async () => {
    useUpdateSkillMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
    resetMock.mockClear();

    renderDrawer(vi.fn(), { ...skill, origin: "store", customizedAt: "2026-11-20T10:00:00Z" });
    fireEvent.click(screen.getByRole("button", { name: "Reset to shipped version" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    expect(resetMock).not.toHaveBeenCalled();
  });

  it("says when a newer shipped version is waiting", () => {
    useUpdateSkillMock.mockReturnValue({ mutate: vi.fn(), isPending: false });

    renderDrawer(vi.fn(), {
      ...skill,
      origin: "store",
      customizedAt: "2026-11-20T10:00:00Z",
      shippedUpdateAvailable: true,
    });

    expect(screen.getByText(/A newer shipped version is available/)).toBeInTheDocument();
  });

  it("duplicates with the localized default name and hands the copy to the page", () => {
    useUpdateSkillMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
    duplicateMock.mockClear();
    const onDuplicated = vi.fn();

    renderDrawer(vi.fn(), skill, onDuplicated);
    fireEvent.click(screen.getByRole("button", { name: "Duplicate" }));

    expect(duplicateMock).toHaveBeenCalledWith({ id: "sk-1", name: "Invoice Review (copy)" });
    expect(onDuplicated).toHaveBeenCalledWith(expect.objectContaining({ id: "sk-2" }));
  });

  it("edits and saves the original text when the list shows a translation", () => {
    const mutate = vi.fn();
    useUpdateSkillMock.mockReturnValue({ mutate, isPending: false });
    publishMock.mockClear();
    const translated: Skill = {
      ...skill,
      name: "Rechnungsprüfung",
      description: "Prüft Rechnungen.",
      instructions: "Positionen extrahieren und buchen.",
      guardrails: ["Beträge über 10 000 € brauchen Freigabe"],
      source: {
        name: skill.name,
        description: skill.description,
        instructions: skill.instructions,
        guardrails: skill.guardrails,
      },
    };

    renderDrawer(vi.fn(), translated);

    expect(screen.getByLabelText("Instructions")).toHaveValue(skill.instructions);
    expect(screen.getByLabelText("Name")).toHaveValue(skill.name);
    expect(
      screen.getByText("You are editing the original text. Translations are not updated."),
    ).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Tools (comma-separated)"), {
      target: { value: "Accounting, OCR" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(publishMock).toHaveBeenCalledWith(
      expect.objectContaining({ instructions: skill.instructions, guardrails: skill.guardrails }),
    );
    expect(mutate).not.toHaveBeenCalled();
  });
});
