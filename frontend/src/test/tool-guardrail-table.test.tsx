import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ToolGuardrailTable, describeGuardrailSaveError } from "@/components/tool-guardrail-table";
import { ApiError } from "@/lib/api";

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useConnectionToolNames: () => ({ data: { names: [] } }),
    useMcpLogins: () => ({ data: [] }),
  };
});

const BLANK = {
  read: false,
  modify: false,
  approvalActions: [],
  approvalEur: null,
  only: [],
  conditions: [],
};
const FULL = { ...BLANK, read: true, modify: true };

describe("ToolGuardrailTable", () => {
  it("renders the Wie Department badge for an inherited row", () => {
    render(
      <ToolGuardrailTable
        level="agent"
        rows={[
          {
            toolKey: "github",
            connection: undefined,
            ceilingPolicy: BLANK,
            ownValue: BLANK,
            status: "inherited",
          },
        ]}
        addableNames={[]}
        connections={[]}
        onSave={vi.fn()}
        onAdd={vi.fn()}
        saving={false}
      />,
    );
    expect(screen.getByText(/wie department|same as department/i)).toBeInTheDocument();
  });

  it("renders the Eingeschränkt badge for a narrowed row", () => {
    render(
      <ToolGuardrailTable
        level="agent"
        rows={[
          {
            toolKey: "github",
            connection: undefined,
            ceilingPolicy: BLANK,
            ownValue: BLANK,
            status: "narrowed",
          },
        ]}
        addableNames={[]}
        connections={[]}
        onSave={vi.fn()}
        onAdd={vi.fn()}
        saving={false}
      />,
    );
    expect(screen.getByText(/eingeschränkt|narrowed/i)).toBeInTheDocument();
  });

  it("renders the Nur dieser Agent badge for an agent-exclusive row", () => {
    render(
      <ToolGuardrailTable
        level="agent"
        rows={[
          {
            toolKey: "internal-wiki",
            connection: undefined,
            ceilingPolicy: null,
            ownValue: BLANK,
            status: "agent-only",
          },
        ]}
        addableNames={[]}
        connections={[]}
        onSave={vi.fn()}
        onAdd={vi.fn()}
        saving={false}
      />,
    );
    expect(screen.getByText(/nur dieser agent|this agent only/i)).toBeInTheDocument();
  });

  it("renders the deviation count on the department view", () => {
    render(
      <ToolGuardrailTable
        level="department"
        rows={[
          {
            toolKey: "github",
            connection: undefined,
            ceilingPolicy: null,
            ownValue: BLANK,
            status: null,
            deviationCount: { count: 3, total: 12 },
          },
        ]}
        addableNames={[]}
        connections={[]}
        onSave={vi.fn()}
        onAdd={vi.fn()}
        saving={false}
      />,
    );
    expect(screen.getByText(/3.*12|3 von 12|3 of 12/i)).toBeInTheDocument();
  });

  it("expands the row into an inline editor and calls onSave with the edited value", () => {
    const onSave = vi.fn();
    render(
      <ToolGuardrailTable
        level="agent"
        rows={[
          {
            toolKey: "github",
            connection: undefined,
            ceilingPolicy: BLANK,
            ownValue: BLANK,
            status: "inherited",
          },
        ]}
        addableNames={[]}
        connections={[]}
        onSave={onSave}
        onAdd={vi.fn()}
        saving={false}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /^(mehr|more)$/i }));
    fireEvent.click(screen.getByRole("button", { name: /speichern|save/i }));
    expect(onSave).toHaveBeenCalledWith("github", expect.any(Object));
  });

  it("collapses the inline editor again on Cancel, leaving the other rows untouched", () => {
    render(
      <ToolGuardrailTable
        level="agent"
        rows={[
          {
            toolKey: "github",
            connection: undefined,
            ceilingPolicy: BLANK,
            ownValue: BLANK,
            status: "inherited",
          },
          {
            toolKey: "odoo",
            connection: undefined,
            ceilingPolicy: BLANK,
            ownValue: BLANK,
            status: "inherited",
          },
        ]}
        addableNames={[]}
        connections={[]}
        onSave={vi.fn()}
        onAdd={vi.fn()}
        saving={false}
      />,
    );
    // No editor open yet -- only the two row-level "Mehr"/"More" buttons exist.
    expect(screen.queryByRole("button", { name: /speichern|save/i })).toBeNull();

    const editButtons = screen.getAllByRole("button", { name: /^(mehr|more)$/i });
    fireEvent.click(editButtons[0]);
    expect(screen.getByRole("button", { name: /speichern|save/i })).toBeInTheDocument();
    // The row being edited itself now reads "Cancel", not "More" -- only one
    // "More" button remains, for the OTHER row, proving this expands in place
    // rather than opening something disconnected from the table.
    expect(screen.getAllByRole("button", { name: /^(mehr|more)$/i })).toHaveLength(1);

    fireEvent.click(screen.getByRole("button", { name: /abbrechen|cancel/i }));
    expect(screen.queryByRole("button", { name: /speichern|save/i })).toBeNull();
    expect(screen.getAllByRole("button", { name: /^(mehr|more)$/i })).toHaveLength(2);
  });

  it("keeps the row expanded when onSave resolves false, instead of discarding the edit", async () => {
    const onSave = vi.fn().mockResolvedValue(false);
    render(
      <ToolGuardrailTable
        level="agent"
        rows={[
          {
            toolKey: "github",
            connection: undefined,
            ceilingPolicy: BLANK,
            ownValue: BLANK,
            status: "inherited",
          },
        ]}
        addableNames={[]}
        connections={[]}
        onSave={onSave}
        onAdd={vi.fn()}
        saving={false}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /^(mehr|more)$/i }));
    fireEvent.click(screen.getByRole("button", { name: /speichern|save/i }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    // A rejected save (e.g. the department's own ceiling doesn't allow the
    // edited value) must not also throw away what the operator was editing --
    // the row stays expanded so they can adjust and retry in place.
    expect(screen.getByRole("button", { name: /speichern|save/i })).toBeInTheDocument();
  });

  it("shows the plain level instead of the Rights and Approval columns", () => {
    for (const level of ["department", "agent"] as const) {
      const { unmount } = render(
        <ToolGuardrailTable
          level={level}
          rows={[
            {
              toolKey: "github",
              connection: undefined,
              ceilingPolicy: null,
              ownValue: FULL,
              status: null,
            },
          ]}
          addableNames={[]}
          connections={[]}
          onSave={vi.fn()}
          onAdd={vi.fn()}
          saving={false}
        />,
      );
      expect(screen.getByRole("button", { name: /github.*allowed/i })).toBeInTheDocument();
      expect(screen.queryByText(/^(rights|rechte)$/i)).toBeNull();
      expect(screen.queryByText(/approval needed for|freigabe nötig für/i)).toBeNull();
      unmount();
    }
  });

  it("shows a quiet limits hint when the policy carries details", () => {
    render(
      <ToolGuardrailTable
        level="department"
        rows={[
          {
            toolKey: "odoo",
            connection: undefined,
            ceilingPolicy: null,
            ownValue: { ...FULL, approvalEur: 5000 },
            status: null,
          },
        ]}
        addableNames={[]}
        connections={[]}
        onSave={vi.fn()}
        onAdd={vi.fn()}
        saving={false}
      />,
    );
    expect(screen.getByText(/with limits|mit Grenzen/i)).toBeInTheDocument();
  });

  it("saves a picked level through onSave without loosening past the ceiling", async () => {
    const onSave = vi.fn().mockResolvedValue(true);
    render(
      <ToolGuardrailTable
        level="agent"
        rows={[
          {
            toolKey: "github",
            connection: undefined,
            ceilingPolicy: { ...FULL, approvalEur: 100 },
            ownValue: { ...FULL, approvalEur: 100 },
            status: "inherited",
          },
        ]}
        addableNames={[]}
        connections={[]}
        onSave={onSave}
        onAdd={vi.fn()}
        saving={false}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /github.*allowed/i }));
    fireEvent.click(await screen.findByRole("radio", { name: /read only/i }));
    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        "github",
        expect.objectContaining({ read: true, modify: false, approvalEur: 100 }),
      ),
    );
  });

  it("greys out levels the department rules out, on the agent level", async () => {
    render(
      <ToolGuardrailTable
        level="agent"
        rows={[
          {
            toolKey: "calendar",
            connection: undefined,
            ceilingPolicy: { ...FULL, modify: false },
            ownValue: { ...FULL, modify: false },
            status: "inherited",
          },
        ]}
        addableNames={[]}
        connections={[]}
        onSave={vi.fn()}
        onAdd={vi.fn()}
        saving={false}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /calendar.*read only/i }));
    expect(
      await screen.findByRole("radio", { name: /needs approval.*set by department/i }),
    ).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("radio", { name: /allowed.*set by department/i })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("shows the member's level, effect sentence and source on level=member", async () => {
    render(
      <ToolGuardrailTable
        level="member"
        rows={[
          {
            toolKey: "gmail",
            connection: undefined,
            ceilingPolicy: FULL,
            ownValue: { ...FULL, approvalActions: ["modify"] },
            status: null,
            effect: "The sales agent asks you before it sends a mail.",
            source: "member",
          },
          {
            toolKey: "calendar",
            connection: undefined,
            ceilingPolicy: { ...FULL, modify: false },
            ownValue: { ...FULL, modify: false },
            status: null,
            source: "agent",
            ceilingSource: "agent",
          },
        ]}
        addableNames={[]}
        connections={[]}
        onSave={vi.fn()}
        onAdd={vi.fn()}
        saving={false}
      />,
    );
    expect(screen.getByRole("button", { name: /gmail.*ask me first/i })).toBeInTheDocument();
    const effect = screen.getByText("The sales agent asks you before it sends a mail.");
    expect(effect.closest("[aria-live]")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByText(/^(you|von dir)$/i)).toBeInTheDocument();
    expect(screen.queryByText(/^(rights|rechte)$/i)).toBeNull();
    // Members cannot add tools of their own.
    expect(screen.queryByRole("button", { name: /add tool|tool hinzufügen/i })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /calendar.*read only/i }));
    expect(await screen.findByRole("radio", { name: /allowed.*set by agent/i })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });
});

describe("describeGuardrailSaveError", () => {
  const t = (en: string, de: string) => en; // English fixed for assertions below.

  it("explains a narrowing_exceeds_frame rejection using the department's own reason", () => {
    const error = new ApiError(
      JSON.stringify({
        error: "narrowing_exceeds_frame",
        violations: [{ tool_key: "github", reason: "'modify' not granted by frame" }],
      }),
      422,
    );
    expect(describeGuardrailSaveError(error, t)).toBe(
      "The department doesn't allow this for github: 'modify' not granted by frame",
    );
  });

  it("explains a value_spec_not_supported rejection by naming the connection", () => {
    const error = new ApiError(
      JSON.stringify({
        error: "value_spec_not_supported",
        violations: [{ connection: "github", field: "approval_eur" }],
      }),
      422,
    );
    expect(describeGuardrailSaveError(error, t)).toBe(
      "github doesn't support a euro threshold for this tool",
    );
  });

  it("shows the fixed security-policy copy for a 403, not the raw permission string", () => {
    const error = new ApiError("requires permission: agent:manage", 403);
    expect(describeGuardrailSaveError(error, t)).toBe(
      "This action is blocked by an OC8 security policy.",
    );
  });

  it("falls back to a generic message for a plain-string or unstructured error", () => {
    expect(describeGuardrailSaveError(new ApiError("agent not found", 404), t)).toBe(
      "agent not found",
    );
    expect(describeGuardrailSaveError(new TypeError("Failed to fetch"), t)).toBe("Failed to fetch");
  });
});
