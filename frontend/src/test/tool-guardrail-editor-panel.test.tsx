import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ToolGuardrailEditorPanel } from "@/components/tool-guardrail-editor-panel";
import type { McpConnection } from "@/lib/hooks";

vi.mock("@/lib/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks")>();
  return {
    ...actual,
    useConnectionToolNames: () => ({ data: { names: ["merge_pr", "delete_branch"] } }),
  };
});

const interpretMutateAsync = vi.fn();
vi.mock("@/lib/hooks-agent-detail", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks-agent-detail")>();
  return {
    ...actual,
    useInterpretGuardrail: () => ({ mutateAsync: interpretMutateAsync }),
    useAgentToolUsage: () => ({
      data: [
        {
          toolKey: "odoo",
          tools: ["merge_pr"],
          per: "day",
          used: 7,
          resetsAt: "2026-11-23T00:00:00+00:00",
        },
        // Another tool's window must not show up under this one.
        {
          toolKey: "email",
          tools: ["merge_pr"],
          per: "day",
          used: 40,
          resetsAt: "2026-11-23T00:00:00+00:00",
        },
      ],
    }),
  };
});

// A preset that actually restricts something -- unlike a blanket "grants
// everything" preset, this decomposes into one per-function row (`merge_pr`,
// approval mode) in the agent-level table below.
const odooConnection = {
  name: "odoo",
  guardrailPresets: [
    {
      key: "merge_needs_approval",
      label: "Merging needs approval",
      labelTranslations: {},
      summary: "Merging a pull request needs approval.",
      summaryTranslations: {},
      recommended: true,
      read: true,
      modify: true,
      approvalActions: ["merge_pr"],
      approvalEur: null,
      only: [],
    },
  ],
  guardrailLibrary: null,
  hasValueSpec: true,
} as unknown as McpConnection;

describe("ToolGuardrailEditorPanel", () => {
  it("shows the department ceiling as read-only context on the agent view", () => {
    render(
      <ToolGuardrailEditorPanel
        toolKey="github"
        connection={undefined}
        ceiling={{
          read: true,
          modify: true,
          approvalActions: ["merge_pr"],
          approvalEur: null,
          only: [],
        }}
        value={{
          read: true,
          modify: false,
          approvalActions: [],
          approvalEur: null,
          only: [],
          conditions: [],
        }}
        onChange={vi.fn()}
        onCancel={vi.fn()}
        onSave={vi.fn()}
        saving={false}
      />,
    );
    expect(screen.getByText(/department erlaubt|department allows/i)).toBeInTheDocument();
  });

  it("shows no department-ceiling section on the department view (ceiling=null)", () => {
    render(
      <ToolGuardrailEditorPanel
        toolKey="github"
        connection={undefined}
        ceiling={null}
        value={{
          read: true,
          modify: true,
          approvalActions: [],
          approvalEur: null,
          only: [],
          conditions: [],
        }}
        onChange={vi.fn()}
        onCancel={vi.fn()}
        onSave={vi.fn()}
        saving={false}
      />,
    );
    expect(screen.queryByText(/department erlaubt|department allows/i)).toBeNull();
  });

  it("offers autocomplete suggestions from the connection's real tool names", () => {
    render(
      <ToolGuardrailEditorPanel
        toolKey="github"
        connection={undefined}
        ceiling={null}
        value={{
          read: true,
          modify: true,
          approvalActions: [],
          approvalEur: null,
          only: [],
          conditions: [],
        }}
        onChange={vi.fn()}
        onCancel={vi.fn()}
        onSave={vi.fn()}
        saving={false}
      />,
    );
    const input = screen.getByRole("combobox", { name: /freigabe|approval/i });
    expect(input.getAttribute("list")).toBeTruthy();
    const listId = input.getAttribute("list") as string;
    expect(document.getElementById(listId)?.innerHTML).toContain("merge_pr");
  });

  it("has a Cancel action that isn't the Save button", () => {
    const onCancel = vi.fn();
    render(
      <ToolGuardrailEditorPanel
        toolKey="github"
        connection={undefined}
        ceiling={null}
        value={{
          read: true,
          modify: true,
          approvalActions: [],
          approvalEur: null,
          only: [],
          conditions: [],
        }}
        onChange={vi.fn()}
        onCancel={onCancel}
        onSave={vi.fn()}
        saving={false}
      />,
    );
    screen.getByRole("button", { name: /abbrechen|cancel/i }).click();
    expect(onCancel).toHaveBeenCalled();
  });

  it("shows the capa's preset as a pre-filled, editable row in the per-function table -- not as a separate card -- when editing for an agent", async () => {
    interpretMutateAsync.mockClear();
    const onChange = vi.fn();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ToolGuardrailEditorPanel
          toolKey="odoo"
          connection={odooConnection}
          ceiling={null}
          value={{
            read: true,
            modify: false,
            approvalActions: [],
            approvalEur: null,
            only: [],
            conditions: [],
          }}
          onChange={onChange}
          onCancel={vi.fn()}
          onSave={vi.fn()}
          saving={false}
          agentId="agent-1"
        />
      </QueryClientProvider>,
    );
    // No full preset card (no "Recommended" badge, no separate summary
    // paragraph) -- the preset only shows up as a row in the same table a
    // manually authored guardrail would use.
    expect(screen.queryByText(/empfohlen|recommended/i)).toBeNull();
    expect(screen.getByText("Merge PR")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Merging a pull request needs approval.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /lesen|read/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /verändern|modify/i })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /neue guardrail|new guardrail/i }),
    ).toBeInTheDocument();

    // Applying an untouched, tool-provided row folds its already-structured
    // policy into the draft directly -- no LLM interpret round-trip needed
    // for it, and no ambiguity with the panel's own bottom Save button since
    // this one is now labelled "Apply"/"Übernehmen" instead.
    within(screen.getByText("Merge PR").closest("tr")!)
      .getByRole("button", { name: /übernehmen|apply/i })
      .click();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(interpretMutateAsync).not.toHaveBeenCalled();
    expect(onChange).toHaveBeenCalledWith({
      read: true,
      modify: false,
      approvalActions: ["merge_pr"],
      approvalEur: null,
      only: [],
      conditions: [],
    });
  });

  it("still shows the capa's preset/library picker as full cards on the department (ceiling) view", () => {
    render(
      <ToolGuardrailEditorPanel
        toolKey="odoo"
        connection={odooConnection}
        ceiling={null}
        value={{
          read: true,
          modify: false,
          approvalActions: [],
          approvalEur: null,
          only: [],
          conditions: [],
        }}
        onChange={vi.fn()}
        onCancel={vi.fn()}
        onSave={vi.fn()}
        saving={false}
      />,
    );
    expect(screen.getByText("Merging needs approval")).toBeInTheDocument();
    expect(screen.getByText("Recommended")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /neue guardrail|new guardrail/i })).toBeNull();
  });

  it("shows the agent's usage of each limit on the agent view", () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <ToolGuardrailEditorPanel
          toolKey="odoo"
          connection={odooConnection}
          ceiling={null}
          value={{
            read: true,
            modify: true,
            approvalActions: [],
            approvalEur: null,
            only: [],
            conditions: [],
            limits: [{ tools: ["merge_pr"], max: 10, per: "day" }],
          }}
          onChange={vi.fn()}
          onCancel={vi.fn()}
          onSave={vi.fn()}
          saving={false}
          agentId="agent-1"
        />
      </QueryClientProvider>,
    );
    expect(screen.getByText(/7 \/ 10/)).toBeInTheDocument();
    expect(screen.queryByText(/40 \/ 10/)).toBeNull();
  });

  it("edits limits on the department view too, without usage", () => {
    render(
      <ToolGuardrailEditorPanel
        toolKey="odoo"
        connection={undefined}
        ceiling={null}
        value={{
          read: true,
          modify: true,
          approvalActions: [],
          approvalEur: null,
          only: [],
          conditions: [],
          limits: [{ tools: ["merge_pr"], max: 10, per: "day" }],
        }}
        onChange={vi.fn()}
        onCancel={vi.fn()}
        onSave={vi.fn()}
        saving={false}
      />,
    );
    expect(
      screen.getByRole("button", { name: /remove limit|limit entfernen/i }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/used today|heute genutzt/i)).toBeNull();
  });
});
