import { describe, expect, it } from "vitest";
import { resolveToolLabel, type ToolLabelCatalogueDTO } from "@/lib/tool-labels";

const t = (en: string, de?: string) => en;
const tDe = (en: string, de?: string) => de ?? en;

const catalogue: ToolLabelCatalogueDTO = {
  connection: "odoo",
  labels: [
    {
      tool: "search_records",
      verb: "Looked up",
      object: "{model_label}",
      running: "Looking up {model_label}",
      verbTranslations: { de: "Nachgesehen" },
      objectTranslations: {},
      runningTranslations: { de: "Sucht {model_label}" },
    },
    {
      tool: "list_models",
      verb: "Checked which data is available",
      object: "",
      running: "Checking which data is available",
      verbTranslations: {},
      objectTranslations: {},
      runningTranslations: {},
    },
    {
      tool: "aggregate_records",
      verb: "Counted",
      object: "{count} {model_label}",
      running: "Counting {model_label}",
      verbTranslations: {},
      objectTranslations: {},
      runningTranslations: {},
    },
  ],
  modelLabels: [{ key: "crm.lead", label: "deals", labelTranslations: { de: "Leads" } }],
  read: ["search_records", "list_models", "aggregate_records"],
  modify: ["create_record", "delete_record"],
};

describe("tier 1 — the pack's declared label", () => {
  it("interpolates a model label from whichever argument carries it", () => {
    const label = resolveToolLabel(
      { tool: "search_records", arguments: { model: "crm.lead" }, connection: "odoo" },
      catalogue,
      t,
      "en",
    );
    expect(label).toBe("Looked up deals");
  });

  it("uses the running form when asked", () => {
    const label = resolveToolLabel(
      {
        tool: "search_records",
        arguments: { model: "crm.lead" },
        connection: "odoo",
        running: true,
      },
      catalogue,
      t,
      "en",
    );
    expect(label).toBe("Looking up deals");
  });

  it("renders German from the pack's own catalog, not a second literal", () => {
    expect(
      resolveToolLabel(
        { tool: "search_records", arguments: { model: "crm.lead" }, connection: "odoo" },
        catalogue,
        tDe,
        "de",
      ),
    ).toBe("Nachgesehen Leads");
    expect(
      resolveToolLabel(
        {
          tool: "search_records",
          arguments: { model: "crm.lead" },
          connection: "odoo",
          running: true,
        },
        catalogue,
        tDe,
        "de",
      ),
    ).toBe("Sucht Leads");
  });

  it("works for a label with no object at all", () => {
    expect(
      resolveToolLabel(
        { tool: "list_models", arguments: {}, connection: "odoo" },
        catalogue,
        t,
        "en",
      ),
    ).toBe("Checked which data is available");
  });

  it("falls back rather than printing an unfillable placeholder", () => {
    // `{count}` has no argument behind it. The spec's own test case.
    const label = resolveToolLabel(
      { tool: "aggregate_records", arguments: { model: "crm.lead" }, connection: "odoo" },
      catalogue,
      t,
      "en",
    );
    expect(label).not.toContain("{");
    expect(label).toBe("Read from odoo");
  });

  it("falls back when the model has no declared label", () => {
    const label = resolveToolLabel(
      { tool: "search_records", arguments: { model: "account.move" }, connection: "odoo" },
      catalogue,
      t,
      "en",
    );
    expect(label).toBe("Read from odoo");
    expect(label).not.toContain("account.move");
  });
});

describe("tier 2 — core control tools", () => {
  it("names a control tool in plain words with no catalogue at all", () => {
    expect(resolveToolLabel({ tool: "memory_write" }, undefined, t, "en")).toBe("Made a note");
    expect(resolveToolLabel({ tool: "ask_user", running: true }, undefined, t, "en")).toBe(
      "Asking a question",
    );
  });

  it("wins over the right-derived fallback even when a catalogue is present", () => {
    expect(resolveToolLabel({ tool: "todo_write", connection: "odoo" }, catalogue, t, "en")).toBe(
      "Updated its plan",
    );
  });

  it("names the five additional control tools", () => {
    expect(resolveToolLabel({ tool: "run_shell" }, undefined, t, "en")).toBe("Ran a command");
    expect(resolveToolLabel({ tool: "run_shell", running: true }, undefined, t, "en")).toBe(
      "Running a command",
    );
  });
});

describe("tier 3 — the right", () => {
  it("says what kind of thing happened for an unlabelled read", () => {
    const sparse: ToolLabelCatalogueDTO = { ...catalogue, labels: [] };
    expect(resolveToolLabel({ tool: "search_records", connection: "odoo" }, sparse, t, "en")).toBe(
      "Read from odoo",
    );
  });

  it("distinguishes a modify from a read", () => {
    expect(
      resolveToolLabel({ tool: "delete_record", connection: "odoo" }, catalogue, t, "en"),
    ).toBe("Changed something in odoo");
  });

  it("never shows a raw name when the right is known", () => {
    for (const tool of [...catalogue.read, ...catalogue.modify]) {
      const label = resolveToolLabel({ tool, connection: "odoo" }, catalogue, t, "en");
      expect(label).not.toBe(tool);
    }
  });
});

describe("last resort", () => {
  it("shows the raw name only when nothing is known about the call", () => {
    expect(resolveToolLabel({ tool: "fs_write", connection: "coding" }, undefined, t, "en")).toBe(
      "fs_write",
    );
  });

  it("never renders an empty label", () => {
    expect(resolveToolLabel({ tool: "" }, undefined, t, "en")).toBe("Did something");
  });
});
