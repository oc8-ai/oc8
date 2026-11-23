import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GuardrailFunctionRules } from "@/components/guardrail-function-rules";
import type { GuardrailValue } from "@/components/guardrail-preset-picker";

const interpretMutateAsync = vi.fn();
const interpretFromInstructionMutateAsync = vi.fn();
vi.mock("@/lib/hooks-agent-detail", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hooks-agent-detail")>();
  return {
    ...actual,
    useInterpretGuardrail: () => ({ mutateAsync: interpretMutateAsync }),
    useInterpretGuardrailsFromInstruction: () => ({
      mutateAsync: interpretFromInstructionMutateAsync,
    }),
  };
});

vi.mock("@/lib/governance-hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/governance-hooks")>();
  return { ...actual, useCan: () => () => true };
});

function baseValue(overrides: Partial<GuardrailValue> = {}): GuardrailValue {
  return {
    read: true,
    modify: true,
    approvalActions: [],
    approvalEur: null,
    only: [],
    conditions: [],
    ...overrides,
  };
}

describe("GuardrailFunctionRules", () => {
  it("shows a row for a function already named in approvalActions", () => {
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue({ approvalActions: ["post_message"] })}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Post Message")).toBeInTheDocument();
    expect(screen.queryByText("Create Ticket")).toBeNull();
  });

  it("shows a row for a function excluded from a non-empty `only`", () => {
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue({ only: ["create_ticket"] })}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Post Message")).toBeInTheDocument();
    expect(screen.queryByText("Create Ticket")).toBeNull();
  });

  it("adds a new row via 'New guardrail', interprets it into a structured preview, and only applies it once accepted", async () => {
    interpretMutateAsync.mockResolvedValueOnce({ decision: "not_allowed", conditions: [] });
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue()}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /new guardrail/i }));
    const select = screen.getByRole("combobox");
    fireEvent.change(select, { target: { value: "post_message" } });

    const row = select.closest("tr")!;
    const input = within(row).getByRole("textbox");
    fireEvent.change(input, { target: { value: "never send on weekends" } });
    fireEvent.click(within(row).getByRole("button", { name: /apply/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /accept/i })).toBeInTheDocument(),
    );
    expect(interpretMutateAsync).toHaveBeenCalledWith({
      connectionName: "odoo",
      function: "post_message",
      definition: "never send on weekends",
    });
    // Not applied yet -- only the interpret call happened so far.
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /accept/i }));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    const next = onChange.mock.calls[0][0] as GuardrailValue;
    expect(next.only).toEqual(["create_ticket"]);
  });

  it("discards a pending interpretation without ever calling onChange", async () => {
    interpretMutateAsync.mockResolvedValueOnce({
      decision: "with_limits",
      conditions: [
        {
          attribute: "order_value",
          datatype: "number",
          operator: ">",
          value: 500,
          then: "require_approval",
        },
      ],
    });
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        guardrailAttributes={[
          {
            key: "order_value",
            label: "Order value",
            labelTranslations: {},
            datatype: "number",
            enumValues: [],
            tools: [],
          },
        ]}
        value={baseValue()}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /new guardrail/i }));
    const select = screen.getByRole("combobox");
    fireEvent.change(select, { target: { value: "post_message" } });
    const row = select.closest("tr")!;
    fireEvent.change(within(row).getByRole("textbox"), {
      target: { value: "up to 500, above that approval" },
    });
    fireEvent.click(within(row).getByRole("button", { name: /apply/i }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /discard/i })).toBeInTheDocument(),
    );
    expect(screen.getByText(/order value/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /discard/i }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /accept/i })).toBeNull();
  });

  it("reverts a function to default-allow when its row is removed", () => {
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue({ approvalActions: ["post_message"] })}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /remove/i }));
    expect(onChange).toHaveBeenCalledWith(baseValue({ approvalActions: [] }));
  });

  it("shows a preset's restricted function as a pre-filled row, saving it without an interpret call", async () => {
    interpretMutateAsync.mockClear();
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue()}
        onChange={onChange}
        presets={[
          {
            key: "reply_needs_approval",
            label: "Replies need approval",
            labelTranslations: {},
            summary: "Sending a message needs approval.",
            summaryTranslations: {},
            recommended: false,
            read: true,
            modify: true,
            approvalActions: ["post_message"],
            approvalEur: null,
            only: [],
          },
        ]}
      />,
    );
    const row = screen.getByText("Post Message").closest("tr")!;
    expect(within(row).getByDisplayValue("Sending a message needs approval.")).toBeInTheDocument();

    fireEvent.click(within(row).getByRole("button", { name: /apply/i }));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    expect(interpretMutateAsync).not.toHaveBeenCalled();
    expect(onChange).toHaveBeenCalledWith(baseValue({ approvalActions: ["post_message"] }));
  });

  it("shows a preset expressed only as `approvalEur` as a 'with limits' row, saving it as a synthesized Condition", async () => {
    interpretMutateAsync.mockClear();
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["create_record", "post_message"]}
        value={baseValue()}
        onChange={onChange}
        guardrailAttributes={[
          {
            key: "order_value",
            label: "Order value",
            labelTranslations: {},
            datatype: "number",
            enumValues: [],
            tools: ["create_record"],
          },
        ]}
        presets={[
          {
            key: "sales_autonomous_with_limit",
            label: "Autonomous up to a limit",
            labelTranslations: {},
            summary: "Creates orders on its own up to a limit, above that approval.",
            summaryTranslations: {},
            recommended: false,
            read: true,
            modify: true,
            approvalActions: [],
            approvalEur: 1000,
            only: [],
          },
        ]}
      />,
    );
    const row = screen.getByText("Create Record").closest("tr")!;
    expect(within(row).getByText(/with limits/i)).toBeInTheDocument();

    fireEvent.click(within(row).getByRole("button", { name: /apply/i }));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    expect(interpretMutateAsync).not.toHaveBeenCalled();
    expect(onChange).toHaveBeenCalledWith(
      baseValue({
        conditions: [
          {
            attribute: "order_value",
            datatype: "number",
            operator: ">=",
            value: 1000,
            then: "require_approval",
          },
        ],
      }),
    );
  });

  it("shows a read-only library entry (no per-function restriction) as a row on the base 'modify' right", async () => {
    interpretMutateAsync.mockClear();
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue()}
        onChange={onChange}
        guardrailLibrary={[
          {
            key: "cross_read_only_everything",
            label: "Read only, across every area",
            labelTranslations: {},
            summary: "Can search and look up, nothing gets changed or sent.",
            summaryTranslations: {},
            useCase: "cross_cutting",
            read: true,
            modify: false,
            approvalEur: null,
            approvalActions: [],
            only: [],
            adjustable: [],
          },
        ]}
      />,
    );
    const row = screen.getByText(/base rights|basisrechte/i).closest("tr")!;
    expect(
      within(row).getByDisplayValue("Can search and look up, nothing gets changed or sent."),
    ).toBeInTheDocument();

    fireEvent.click(within(row).getByRole("button", { name: /apply/i }));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    expect(interpretMutateAsync).not.toHaveBeenCalled();
    expect(onChange).toHaveBeenCalledWith(baseValue({ modify: false }));
  });

  it("suggests each function once, however many presets restrict it (E2E F4)", () => {
    const entry = (key: string, label: string, summary: string, only: string[]) => ({
      key,
      label,
      labelTranslations: {},
      summary,
      summaryTranslations: {},
      useCase: "cross_cutting",
      read: true,
      modify: true,
      approvalEur: null,
      approvalActions: [],
      only,
      adjustable: [],
    });
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "delete_record", "move_record"]}
        value={baseValue()}
        onChange={vi.fn()}
        guardrailLibrary={[
          entry("cross_never_deletes", "Never delete", "delete_record is never available.", [
            "post_message",
            "move_record",
          ]),
          entry("finance_no_deletions", "Finance", "Withheld for the audit trail.", [
            "post_message",
          ]),
          entry("support", "Support", "Only replies.", ["post_message"]),
        ]}
      />,
    );
    expect(screen.getAllByText("Delete Record")).toHaveLength(1);
    expect(screen.getAllByText("Move Record")).toHaveLength(1);
    // The first preset that proposes it speaks for the row.
    expect(screen.getByDisplayValue("delete_record is never available.")).toBeInTheDocument();
  });

  it("applies a whole preset, its usage limit included (E2E F4)", () => {
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="email"
        toolCatalog={["mail_get", "mail_send", "mail_delete"]}
        value={baseValue()}
        onChange={onChange}
        presets={[
          {
            key: "send_with_approval",
            label: "Send with approval",
            labelTranslations: {},
            summary: "Every mail waits for approval.",
            summaryTranslations: {},
            recommended: true,
            read: true,
            modify: true,
            approvalActions: ["mail_send"],
            approvalEur: null,
            only: [],
            limits: [],
          },
          {
            key: "autonomous_with_limit",
            label: "Autonomous, max 50 mails a day",
            labelTranslations: { de: "Selbstständig, max. 50 Mails am Tag" },
            summary: "Works on its own, up to 50 mails a day.",
            summaryTranslations: {},
            recommended: false,
            read: true,
            modify: true,
            approvalActions: [],
            approvalEur: null,
            only: ["mail_get", "mail_send"],
            limits: [{ tools: ["mail_send"], max: 50, per: "day" }],
          },
        ]}
      />,
    );
    fireEvent.change(screen.getByRole("combobox", { name: /apply a preset/i }), {
      target: { value: "autonomous_with_limit" },
    });
    expect(onChange).toHaveBeenCalledWith(
      baseValue({
        only: ["mail_get", "mail_send"],
        limits: [{ tools: ["mail_send"], max: 50, per: "day" }],
      }),
    );
  });

  it("dismisses a suggested row without touching the shared value", () => {
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue()}
        onChange={onChange}
        presets={[
          {
            key: "reply_needs_approval",
            label: "Replies need approval",
            labelTranslations: {},
            summary: "Sending a message needs approval.",
            summaryTranslations: {},
            recommended: false,
            read: true,
            modify: true,
            approvalActions: ["post_message"],
            approvalEur: null,
            only: [],
          },
        ]}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /remove/i }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByText("Post Message")).toBeNull();
  });

  it("toggles a row's checkbox straight to allow/deny, with no interpret call", () => {
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue({ approvalActions: ["post_message"] })}
        onChange={onChange}
      />,
    );
    const row = screen.getByText("Post Message").closest("tr")!;
    // `post_message` isn't in `readTools`, so it's classified "modify" --
    // that's the one live checkbox on this row; "Read" is the disabled,
    // forced-implied one (see the new two-checkbox-per-row test below).
    const checkbox = within(row).getByRole("checkbox", { name: /modify|verändern/i });
    expect(checkbox).not.toBeDisabled();
    fireEvent.click(checkbox);
    expect(interpretMutateAsync).not.toHaveBeenCalled();
    expect(onChange).toHaveBeenCalledWith(
      baseValue({ approvalActions: [], only: ["create_ticket"] }),
    );
  });

  it("always renders one pinned base-rights row with live, enabled Read and Modify checkboxes", () => {
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue()}
        onChange={vi.fn()}
      />,
    );
    const gateRow = screen.getByText(/base rights|basisrechte/i).closest("tr")!;
    const checkboxes = within(gateRow).getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    checkboxes.forEach((checkbox) => expect(checkbox).not.toBeDisabled());
  });

  it("unchecking the pinned row's modify checkbox denies the base right directly", () => {
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue()}
        onChange={onChange}
      />,
    );
    const gateRow = screen.getByText(/base rights|basisrechte/i).closest("tr")!;
    fireEvent.click(within(gateRow).getByRole("checkbox", { name: /^modify$|^verändern$/i }));
    expect(onChange).toHaveBeenCalledWith(baseValue({ modify: false }));
  });

  it("checking modify on the pinned row also grants read (modify implies read)", () => {
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue({ read: false, modify: false })}
        onChange={onChange}
      />,
    );
    const gateRow = screen.getByText(/base rights|basisrechte/i).closest("tr")!;
    fireEvent.click(within(gateRow).getByRole("checkbox", { name: /^modify$|^verändern$/i }));
    expect(onChange).toHaveBeenCalledWith(baseValue({ read: true, modify: true }));
  });

  it("unchecking read on the pinned row also revokes modify (modify implies read)", () => {
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue({ read: true, modify: true })}
        onChange={onChange}
      />,
    );
    const gateRow = screen.getByText(/base rights|basisrechte/i).closest("tr")!;
    fireEvent.click(within(gateRow).getByRole("checkbox", { name: /^read$|^lesen$/i }));
    expect(onChange).toHaveBeenCalledWith(baseValue({ read: false, modify: false }));
  });

  it("filters per-function rows by right while always keeping the gate row visible", () => {
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["search_records", "create_record"]}
        readTools={["search_records"]}
        value={baseValue({ approvalActions: ["search_records", "create_record"] })}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Search Records")).toBeInTheDocument();
    expect(screen.getByText("Create Record")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^read$|^lesen$/i }));
    expect(screen.getByText("Search Records")).toBeInTheDocument();
    expect(screen.queryByText("Create Record")).toBeNull();
    expect(screen.getByText(/base rights|basisrechte/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^read$|^lesen$/i }));
    expect(screen.getByText("Create Record")).toBeInTheDocument();
  });

  it("prefills a gate-targeting predefined guardrail's text onto the pinned gate row, not a duplicate row", () => {
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["search_records", "create_record"]}
        value={baseValue()}
        onChange={vi.fn()}
        guardrailLibrary={[
          {
            key: "cross_read_only_everything",
            label: "Read only, across every area",
            labelTranslations: {},
            summary: "Can search and look up, nothing gets changed or sent.",
            summaryTranslations: {},
            useCase: "cross_cutting",
            read: true,
            modify: false,
            approvalEur: null,
            approvalActions: [],
            only: [],
            adjustable: [],
          },
        ]}
      />,
    );
    expect(screen.getAllByText(/base rights|basisrechte/i)).toHaveLength(1);
  });

  it("shows both Read and Modify checkboxes on a per-function row, disabling the one that doesn't apply", () => {
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "search_records"]}
        readTools={["search_records"]}
        value={baseValue({ approvalActions: ["post_message", "search_records"] })}
        onChange={vi.fn()}
      />,
    );

    // `post_message` is classified "modify" -- Modify is live, Read is
    // disabled and forced on (modify implies read).
    const modifyRow = screen.getByText("Post Message").closest("tr")!;
    const modifyRowChecked = within(modifyRow).getByRole("checkbox", { name: /modify|verändern/i });
    const modifyRowRead = within(modifyRow).getByRole("checkbox", { name: /^read$|^lesen$/i });
    expect(modifyRowChecked).not.toBeDisabled();
    expect(modifyRowRead).toBeDisabled();
    expect(modifyRowRead).toBeChecked();

    // `search_records` is classified "read" -- Read is live, Modify is
    // disabled and forced off (a read-only function has no modify action).
    const readRow = screen.getByText("Search Records").closest("tr")!;
    const readRowChecked = within(readRow).getByRole("checkbox", { name: /^read$|^lesen$/i });
    const readRowModify = within(readRow).getByRole("checkbox", { name: /modify|verändern/i });
    expect(readRowChecked).not.toBeDisabled();
    expect(readRowModify).toBeDisabled();
    expect(readRowModify).not.toBeChecked();
  });

  it("keeps a row visible after ticking its checkbox to allow, instead of it disappearing mid-click", () => {
    const onChange = vi.fn();
    // `post_message` starts denied (excluded from a non-empty `only`), so
    // its checkbox starts unchecked.
    const { rerender } = render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue({ only: ["create_ticket"] })}
        onChange={onChange}
      />,
    );
    const row = screen.getByText("Post Message").closest("tr")!;
    const checkbox = within(row).getByRole("checkbox", { name: /modify|verändern/i });
    expect(checkbox).not.toBeChecked();
    fireEvent.click(checkbox);
    const next = onChange.mock.calls[0][0] as GuardrailValue;
    expect(next.only).toContain("post_message");

    // The live `value` this component receives from its parent updates to
    // reflect the click (mode now "allow") -- exactly what happens on the
    // very next render in the real app. The row must not vanish.
    rerender(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={next}
        onChange={onChange}
      />,
    );
    expect(screen.getByText("Post Message")).toBeInTheDocument();

    // Only the explicit Remove button clears it back out.
    fireEvent.click(
      within(screen.getByText("Post Message").closest("tr")!).getByRole("button", {
        name: /remove/i,
      }),
    );
    expect(screen.queryByText("Post Message")).toBeNull();
  });

  it("lets the copilot suggest guardrails from the instructions, previewed until accepted", async () => {
    interpretFromInstructionMutateAsync.mockResolvedValueOnce({
      results: [{ function: "delete_record", decision: "not_allowed", conditions: [] }],
    });
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "delete_record"]}
        value={baseValue()}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /suggest from instructions/i }));
    expect(interpretFromInstructionMutateAsync).toHaveBeenCalledWith({ connectionName: "odoo" });

    await waitFor(() => expect(screen.getByText("Delete Record")).toBeInTheDocument());
    expect(screen.getByText(/not allowed/i, { ignore: "textarea" })).toBeInTheDocument();
    // A preview only -- nothing applied to the draft until it's accepted.
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /accept/i }));
    await waitFor(() => expect(onChange).toHaveBeenCalled());
    const next = onChange.mock.calls[0][0] as GuardrailValue;
    expect(next.only).toEqual(["post_message"]);
  });

  it("discarding a copilot suggestion never touches the draft", async () => {
    interpretFromInstructionMutateAsync.mockResolvedValueOnce({
      results: [{ function: "delete_record", decision: "not_allowed", conditions: [] }],
    });
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "delete_record"]}
        value={baseValue()}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /suggest from instructions/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /discard/i })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /discard/i }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /accept/i })).toBeNull();
  });

  it("tells the operator when the copilot finds nothing to restrict", async () => {
    interpretFromInstructionMutateAsync.mockResolvedValueOnce({ results: [] });
    const onChange = vi.fn();
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "delete_record"]}
        value={baseValue()}
        onChange={onChange}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /suggest from instructions/i }));
    await waitFor(() => expect(interpretFromInstructionMutateAsync).toHaveBeenCalled());
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /accept/i })).toBeNull();
  });

  it("opens the definition in a larger editor and keeps the text in step", () => {
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue()}
        onChange={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /new guardrail/i }));
    const row = screen.getByRole("combobox").closest("tr")!;
    const field = within(row).getByRole("textbox") as HTMLTextAreaElement;
    expect(field.tagName).toBe("TEXTAREA");

    fireEvent.click(within(row).getByRole("button", { name: /larger editor/i }));
    const dialog = screen.getByRole("dialog");
    const large = within(dialog).getByRole("textbox") as HTMLTextAreaElement;
    const text = "up to 100 units on its own,\nabove that approval by the team lead";
    fireEvent.change(large, { target: { value: text } });
    fireEvent.click(within(dialog).getByRole("button", { name: /done/i }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect((within(row).getByRole("textbox") as HTMLTextAreaElement).value).toBe(text);
  });

  it("applies on Enter and starts a new line on Shift+Enter", async () => {
    interpretMutateAsync.mockResolvedValueOnce({ decision: "not_allowed", conditions: [] });
    render(
      <GuardrailFunctionRules
        agentId="a1"
        connectionName="odoo"
        toolCatalog={["post_message", "create_ticket"]}
        value={baseValue()}
        onChange={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /new guardrail/i }));
    const row = screen.getByRole("combobox").closest("tr")!;
    const field = within(row).getByRole("textbox");
    fireEvent.change(field, { target: { value: "never send on weekends" } });

    fireEvent.keyDown(field, { key: "Enter", shiftKey: true });
    expect(interpretMutateAsync).not.toHaveBeenCalled();

    fireEvent.keyDown(field, { key: "Enter" });
    await waitFor(() => expect(interpretMutateAsync).toHaveBeenCalledTimes(1));
  });
});
