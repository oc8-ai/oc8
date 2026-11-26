import { render, screen, fireEvent } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";
import {
  GuardrailPresetPicker,
  type GuardrailLibraryEntry,
  type GuardrailPreset,
  type GuardrailValue,
} from "@/components/guardrail-preset-picker";
import { LanguageProvider } from "@/lib/i18n";

const PRESETS: GuardrailPreset[] = [
  {
    key: "read_only",
    label: "Read only",
    labelTranslations: { de: "Nur lesen" },
    summary: "Can search.",
    summaryTranslations: { de: "s" },
    recommended: false,
    read: true,
    modify: false,
    approvalActions: [],
    approvalEur: null,
    only: [],
  },
  {
    key: "assist_with_approval",
    label: "Assist with approval",
    labelTranslations: { de: "Unterstützen" },
    summary: "Every send needs approval.",
    summaryTranslations: { de: "s" },
    recommended: true,
    read: true,
    modify: true,
    approvalActions: ["modify"],
    approvalEur: null,
    only: [],
  },
];

// The property this whole feature hinges on: `autonomous_with_limit` is safe
// ONLY because it withholds `delete_record` -- a deletion carries no amount
// and so can never meet its EUR threshold. If a picker ever emitted the
// read/modify/approval bits but dropped `only`, this preset would apply
// as "everything reachable above EUR 1000", including unattended deletion.
const AUTONOMOUS_WITH_LIMIT: GuardrailPreset = {
  key: "autonomous_with_limit",
  label: "Autonomous with a limit",
  labelTranslations: { de: "Autonom mit Limit" },
  summary: "Acts on its own below the threshold.",
  summaryTranslations: { de: "s" },
  recommended: false,
  read: true,
  modify: true,
  approvalActions: [],
  approvalEur: 1000,
  only: ["search_records", "update_record"],
};

const FREE: GuardrailValue = {
  read: true,
  modify: false,
  approvalActions: [],
  approvalEur: null,
  only: [],
  conditions: [],
};

describe("GuardrailPresetPicker", () => {
  it("lists presets with the recommended one first and marked", () => {
    render(
      <GuardrailPresetPicker
        presets={PRESETS}
        hasValueSpec={false}
        value={FREE}
        onChange={vi.fn()}
      />,
    );
    const options = screen.getAllByRole("button", { name: /Read only|Assist with approval/i });
    expect(options[0]).toHaveTextContent(/Assist with approval/i);
    expect(screen.getByText("Recommended")).toBeInTheDocument();
  });

  it("renders the free controls with no chooser when the plugin ships no presets", () => {
    render(
      <GuardrailPresetPicker presets={[]} hasValueSpec={false} value={FREE} onChange={vi.fn()} />,
    );
    expect(screen.queryByText(/Read only/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Modify" })).toBeInTheDocument();
  });

  it("selecting a preset applies its policy via onChange", () => {
    const onChange = vi.fn();
    render(
      <GuardrailPresetPicker
        presets={PRESETS}
        hasValueSpec={false}
        value={FREE}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByText(/Assist with approval/i));
    expect(onChange).toHaveBeenCalledWith({
      read: true,
      modify: true,
      approvalActions: ["modify"],
      approvalEur: null,
      only: [],
      conditions: [],
      limits: [],
    });
  });

  it("carries `only` through selection so a preset's allowlist reaches the caller", () => {
    const onChange = vi.fn();
    render(
      <GuardrailPresetPicker
        presets={[AUTONOMOUS_WITH_LIMIT]}
        hasValueSpec
        value={FREE}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByText(/Autonomous with a limit/i));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ only: ["search_records", "update_record"] }),
    );
    // The delete tool must never be part of the applied allowlist -- that
    // absence is the entire safety argument for this preset.
    const applied = onChange.mock.calls[0][0] as GuardrailValue;
    expect(applied.only).not.toContain("delete_record");
  });

  it("stops highlighting a preset as selected once a control is edited away from it", () => {
    const onChange = vi.fn();
    const applied: GuardrailValue = {
      read: true,
      modify: true,
      approvalActions: ["modify"],
      approvalEur: null,
      only: [],
    };
    const { rerender } = render(
      <GuardrailPresetPicker
        presets={PRESETS}
        hasValueSpec={false}
        value={applied}
        onChange={onChange}
      />,
    );
    expect(screen.getByText(/Assist with approval/i).closest("button")).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    const edited = { ...applied, modify: false };
    rerender(
      <GuardrailPresetPicker
        presets={PRESETS}
        hasValueSpec={false}
        value={edited}
        onChange={onChange}
      />,
    );
    expect(screen.getByText(/Assist with approval/i).closest("button")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("dropping a preset's `only` restriction (widening it) also stops highlighting it as selected", () => {
    const widened: GuardrailValue = {
      read: AUTONOMOUS_WITH_LIMIT.read,
      modify: AUTONOMOUS_WITH_LIMIT.modify,
      approvalActions: AUTONOMOUS_WITH_LIMIT.approvalActions,
      approvalEur: AUTONOMOUS_WITH_LIMIT.approvalEur,
      only: [],
    };
    render(
      <GuardrailPresetPicker
        presets={[AUTONOMOUS_WITH_LIMIT]}
        hasValueSpec
        value={widened}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText(/Autonomous with a limit/i).closest("button")).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("keeps the free controls visible and pre-filled even when the value exactly matches a preset -- applying a preset is not a locked state", () => {
    const applied: GuardrailValue = {
      read: true,
      modify: true,
      approvalActions: ["modify"],
      approvalEur: null,
      only: [],
    };
    render(
      <GuardrailPresetPicker
        presets={PRESETS}
        hasValueSpec={false}
        value={applied}
        onChange={vi.fn()}
      />,
    );
    // Pre-filled from the matching preset: the "modify" checkbox (its one
    // approval action) is already checked, with no extra click required.
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes.length).toBeGreaterThan(0);
    expect((checkboxes[0] as HTMLInputElement).checked).toBe(true);
  });

  it("editing a control while a preset's values are still applied propagates the edit, not a reset", () => {
    const onChange = vi.fn();
    const applied: GuardrailValue = {
      read: true,
      modify: true,
      approvalActions: ["modify"],
      approvalEur: null,
      only: [],
    };
    render(
      <GuardrailPresetPicker
        presets={PRESETS}
        hasValueSpec={false}
        value={applied}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByRole("checkbox"));
    expect(onChange).toHaveBeenCalledWith({ ...applied, approvalActions: [] });
  });

  it("shows the euro field only when hasValueSpec is true", () => {
    const { rerender } = render(
      <GuardrailPresetPicker presets={[]} hasValueSpec={false} value={FREE} onChange={vi.fn()} />,
    );
    expect(screen.queryByLabelText(/€/i)).not.toBeInTheDocument();
    rerender(<GuardrailPresetPicker presets={[]} hasValueSpec value={FREE} onChange={vi.fn()} />);
    expect(screen.getByLabelText(/€/i)).toBeInTheDocument();
  });

  it("always offers an approval-actions control, regardless of hasValueSpec", () => {
    render(
      <GuardrailPresetPicker presets={[]} hasValueSpec={false} value={FREE} onChange={vi.fn()} />,
    );
    expect(screen.getByText(/Approval needed for:/i)).toBeInTheDocument();
  });

  it("lets an operator type an arbitrary tool name and adds it to approvalActions on Enter", () => {
    const onChange = vi.fn();
    render(
      <GuardrailPresetPicker presets={[]} hasValueSpec={false} value={FREE} onChange={onChange} />,
    );
    const input = screen.getByPlaceholderText(/delete_record/i);
    fireEvent.change(input, { target: { value: "delete_record" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith({
      ...FREE,
      approvalActions: [...FREE.approvalActions, "delete_record"],
    });
  });

  it("renders a free-text approval action as a removable chip, leaving modify untouched", () => {
    const onChange = vi.fn();
    const value: GuardrailValue = { ...FREE, approvalActions: ["modify", "delete_record"] };
    render(
      <GuardrailPresetPicker presets={[]} hasValueSpec={false} value={value} onChange={onChange} />,
    );
    expect(screen.getByText("delete_record")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /remove delete_record/i }));
    expect(onChange).toHaveBeenCalledWith({ ...value, approvalActions: ["modify"] });
  });
});

// Realistic shapes lifted from plugins/odoo_mcp/guardrails/ (design §7):
// two use_cases, one entry with an `approval_eur` default set, one with none.
// Deliberately NOT shared with `PRESETS`/`GENERIC_FIVE` below -- these
// fixtures exist only to exercise the library branch.
const LIBRARY: GuardrailLibraryEntry[] = [
  {
    key: "sales_quote_approval_threshold",
    label: "Approve quotes above an amount",
    labelTranslations: { de: "Angebote ab einem Betrag freigeben" },
    summary: "Creates and maintains quotes on its own; above the amount a human decides.",
    summaryTranslations: { de: "s" },
    useCase: "sales",
    read: true,
    modify: true,
    approvalEur: 3000,
    approvalActions: [],
    only: [],
    adjustable: [
      {
        field: "approval_eur",
        label: "Approval from",
        labelTranslations: { de: "Freigabe ab" },
        unit: "EUR",
        min: 0,
        max: 50000,
      },
    ],
  },
  {
    key: "helpdesk_reply_needs_approval",
    label: "Work tickets, customer replies need approval",
    labelTranslations: { de: "Tickets bearbeiten, Kundenantwort mit Freigabe" },
    summary: "Can search and update tickets; sending a reply needs approval.",
    summaryTranslations: { de: "s" },
    useCase: "helpdesk",
    read: true,
    modify: true,
    approvalEur: null,
    approvalActions: ["post_message"],
    only: [],
    adjustable: [],
  },
];

describe("GuardrailPresetPicker -- guardrail library (grouped by use_case)", () => {
  it("groups library entries under bilingual use_case headings and does NOT render the generic presets alongside them", () => {
    render(
      <GuardrailPresetPicker
        presets={PRESETS}
        guardrailLibrary={LIBRARY}
        hasValueSpec
        value={FREE}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Sales")).toBeInTheDocument();
    expect(screen.getByText("Helpdesk")).toBeInTheDocument();
    expect(screen.getByText(/Approve quotes above an amount/i)).toBeInTheDocument();
    expect(screen.getByText(/Work tickets, customer replies need approval/i)).toBeInTheDocument();
    // Design §6: a library REPLACES the generic presets, never alongside them.
    expect(screen.queryByText(/Read only/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Assist with approval/i)).not.toBeInTheDocument();
  });

  it("renders a tool-name approval_actions entry as the raw tool name, not the bilingual 'sends' right label", () => {
    // Regression for the final-review Critical 2 finding: unlike
    // `GuardrailPreset.approvalActions` (generic-preset branch), a library
    // `Guardrail.approval_actions` can legitimately name a real tool --
    // `helpdesk_reply_needs_approval` ships `approval_actions =
    // ["post_message"]` specifically so OTHER sends stay ungated. Rendering
    // it as "sends" claims every send is gated, which is exactly backwards
    // for this entry's purpose.
    render(
      <GuardrailPresetPicker
        presets={[]}
        guardrailLibrary={LIBRARY}
        hasValueSpec
        value={FREE}
        onChange={vi.fn()}
      />,
    );
    const chip = screen.getByText(/Approval for:/i).closest("span");
    expect(chip).toHaveTextContent("post_message");
    expect(chip).not.toHaveTextContent(/sends/i);
  });

  it("selecting a library entry applies its policy, with the shared euro field picking up its default", () => {
    const onChange = vi.fn();
    const { rerender } = render(
      <GuardrailPresetPicker
        presets={[]}
        guardrailLibrary={LIBRARY}
        hasValueSpec
        value={FREE}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByText(/Approve quotes above an amount/i));
    expect(onChange).toHaveBeenCalledWith({
      read: true,
      modify: true,
      approvalActions: [],
      approvalEur: 3000,
      only: [],
      conditions: [],
      limits: [],
    });
    const applied = onChange.mock.calls[0][0] as GuardrailValue;

    // The picker is controlled -- feed the applied value back in, as the real
    // consumers do via their own `onChange` state, to see the shared euro
    // field reflect it. There is no separate per-entry "adjustable" widget
    // any more -- the one euro field, always visible, is the only place
    // `approval_eur` is edited regardless of which entry is selected.
    rerender(
      <GuardrailPresetPicker
        presets={[]}
        guardrailLibrary={LIBRARY}
        hasValueSpec
        value={applied}
        onChange={onChange}
      />,
    );
    expect(screen.getByLabelText(/€/i)).toHaveValue(3000);
    expect(screen.queryByLabelText(/Approval from/i)).not.toBeInTheDocument();
  });

  it("`only` survives selecting a library entry into the applied policy, same as a generic preset", () => {
    const onChange = vi.fn();
    const restrictive: GuardrailLibraryEntry = {
      ...LIBRARY[0],
      key: "sales_autonomous_with_limit",
      only: ["search_records", "update_record"],
    };
    render(
      <GuardrailPresetPicker
        presets={[]}
        guardrailLibrary={[restrictive]}
        hasValueSpec
        value={FREE}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByText(/Approve quotes above an amount/i));
    const applied = onChange.mock.calls[0][0] as GuardrailValue;
    expect(applied.only).toEqual(["search_records", "update_record"]);
    expect(applied.only).not.toContain("delete_record");
  });

  it("the free-text approval-action input is available in the library branch too, without any extra click", () => {
    const onChange = vi.fn();
    render(
      <GuardrailPresetPicker
        presets={[]}
        guardrailLibrary={LIBRARY}
        hasValueSpec
        value={FREE}
        onChange={onChange}
      />,
    );
    const input = screen.getByPlaceholderText(/delete_record/i);
    fireEvent.change(input, { target: { value: "delete_record" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).toHaveBeenCalledWith({
      ...FREE,
      approvalActions: [...FREE.approvalActions, "delete_record"],
    });
  });

  it("renders a raw use_case string humanized when it has no bilingual entry in the known table", () => {
    const custom: GuardrailLibraryEntry = { ...LIBRARY[0], key: "x", useCase: "warehouse_ops" };
    render(
      <GuardrailPresetPicker
        presets={[]}
        guardrailLibrary={[custom]}
        hasValueSpec
        value={FREE}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText("Warehouse Ops")).toBeInTheDocument();
  });

  it("renders bilingually under the German locale -- no raw English leaks through", () => {
    window.localStorage.setItem("oc8-lang", "de");
    try {
      const applied: GuardrailValue = {
        read: true,
        modify: true,
        approvalActions: [],
        approvalEur: 3000,
        only: [],
      };
      render(
        <LanguageProvider>
          <GuardrailPresetPicker
            presets={[]}
            guardrailLibrary={LIBRARY}
            hasValueSpec
            value={applied}
            onChange={vi.fn()}
          />
        </LanguageProvider>,
      );
      // use_case heading, entry copy, and the always-visible free editor's
      // own labels all have a German pair and must use it -- not the
      // English fallback.
      expect(screen.getByText("Vertrieb")).toBeInTheDocument();
      expect(screen.getByText("Angebote ab einem Betrag freigeben")).toBeInTheDocument();
      expect(screen.getByText(/Freigabe nötig für:/i)).toBeInTheDocument();
      expect(screen.queryByText(/Approve quotes above an amount/i)).not.toBeInTheDocument();
    } finally {
      window.localStorage.removeItem("oc8-lang");
    }
  });
});

// Five distinct fixtures dedicated to the no-library regression case, never
// imported by any library-mode test above -- sharing objects between the two
// could let a bug that leaks library state into the fallback path (or vice
// versa) pass unnoticed.
const GENERIC_FIVE: GuardrailPreset[] = [
  {
    key: "read_only",
    label: "Read only",
    labelTranslations: { de: "Nur lesen" },
    summary: "Can search.",
    summaryTranslations: { de: "s" },
    recommended: false,
    read: true,
    modify: false,
    approvalActions: [],
    approvalEur: null,
    only: [],
  },
  {
    key: "assist_with_approval",
    label: "Assist with approval",
    labelTranslations: { de: "Unterstützen" },
    summary: "Every send needs approval.",
    summaryTranslations: { de: "s" },
    recommended: true,
    read: true,
    modify: true,
    approvalActions: ["modify"],
    approvalEur: null,
    only: [],
  },
  {
    key: "autonomous_with_limit",
    label: "Autonomous with a limit",
    labelTranslations: { de: "Autonom mit Limit" },
    summary: "Acts on its own below the threshold.",
    summaryTranslations: { de: "s" },
    recommended: false,
    read: true,
    modify: true,
    approvalActions: [],
    approvalEur: 1000,
    only: ["search_records", "update_record"],
  },
  {
    key: "internal_only",
    label: "Internal only",
    labelTranslations: { de: "Nur intern" },
    summary: "Never messages a customer.",
    summaryTranslations: { de: "s" },
    recommended: false,
    read: true,
    modify: true,
    approvalActions: [],
    approvalEur: null,
    only: ["search_records", "get_record", "create_record", "update_record"],
  },
  {
    key: "no_deletions",
    label: "No deletions",
    labelTranslations: { de: "Nie löschen" },
    summary: "Never deletes a record.",
    summaryTranslations: { de: "s" },
    recommended: false,
    read: true,
    modify: true,
    approvalActions: [],
    approvalEur: null,
    only: ["search_records", "get_record", "create_record", "update_record", "post_message"],
  },
];

describe("GuardrailPresetPicker -- no library (backward-compat regression)", () => {
  afterEach(() => {
    window.localStorage.removeItem("oc8-lang");
  });

  it("renders exactly today's five generic presets when guardrailLibrary is absent, plus the always-visible free editor", () => {
    render(
      <GuardrailPresetPicker presets={GENERIC_FIVE} hasValueSpec value={FREE} onChange={vi.fn()} />,
    );
    for (const preset of GENERIC_FIVE) {
      expect(screen.getByText(new RegExp(preset.label, "i"))).toBeInTheDocument();
    }
    // No library grouping ever appears when there is no library.
    expect(screen.queryByText("Sales")).not.toBeInTheDocument();
    expect(screen.queryByText("Helpdesk")).not.toBeInTheDocument();
    // The free editor is always present now, not gated behind a separate
    // "Configure myself" mode.
    expect(screen.getByRole("button", { name: "Modify" })).toBeInTheDocument();
  });

  it("renders exactly today's five generic presets when guardrailLibrary is explicitly null", () => {
    render(
      <GuardrailPresetPicker
        presets={GENERIC_FIVE}
        guardrailLibrary={null}
        hasValueSpec
        value={FREE}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText(/Read only/i)).toBeInTheDocument();
    expect(screen.getByText(/No deletions/i)).toBeInTheDocument();
  });

  it("falls back to the generic presets when guardrailLibrary is an empty array", () => {
    render(
      <GuardrailPresetPicker
        presets={GENERIC_FIVE}
        guardrailLibrary={[]}
        hasValueSpec
        value={FREE}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText(/Read only/i)).toBeInTheDocument();
  });

  it("selecting a generic preset still applies its policy, unaffected by this feature", () => {
    const onChange = vi.fn();
    render(
      <GuardrailPresetPicker
        presets={GENERIC_FIVE}
        hasValueSpec
        value={FREE}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByText(/Autonomous with a limit/i));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ approvalEur: 1000, only: ["search_records", "update_record"] }),
    );
  });
});
