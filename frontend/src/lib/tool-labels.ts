// What a tool call says to an end user. `search_records` is what the model
// called; "Looked up deals" is what a salesperson reads.
//
// Three tiers, in this order, and the order is the whole design:
//   1. the tool pack's own declared label (capas/*/tool_pack.toml's
//      [connections.tool_labels], served by GET /mcp/connections/{name}/tool-labels),
//   2. a core control tool's own sentence -- memory_write and ask_user are
//      oc8's tools, not a vendor's, so naming them here breaks no boundary,
//   3. the tool's RIGHT: "Read from odoo" / "Changed something in odoo".
// Only when even the connection is unknown does the raw name appear. A user
// who reads "Changed something in odoo" learns more than one who reads
// `write`, and the fallback costs nothing.
//
// No vendor string appears in this file. `{model_label}` is resolved by
// looking up a call's argument VALUES in the pack's own model_labels map --
// never by reaching for an argument called `model`, which would put an Odoo
// assumption in the frontend.

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { resolveTranslation, type Lang } from "@/lib/i18n";

export interface ToolLabelDTO {
  tool: string;
  verb: string;
  object: string;
  running: string;
  verbTranslations: Record<string, string>;
  objectTranslations: Record<string, string>;
  runningTranslations: Record<string, string>;
}

export interface ModelLabelDTO {
  key: string;
  label: string;
  labelTranslations: Record<string, string>;
}

export interface ToolLabelCatalogueDTO {
  connection: string;
  labels: ToolLabelDTO[];
  modelLabels: ModelLabelDTO[];
  read: string[];
  modify: string[];
}

/** `useT()`'s return type, passed in rather than called here: this module is
 *  a plain function so the resolution order can be unit-tested without a
 *  React tree. Same arrangement `formatReasonContext` in approvals-widget.tsx
 *  already uses. */
export type Translate = (en: string, de?: string) => string;

export interface LabelledCall {
  tool: string;
  arguments?: Record<string, unknown>;
  /** The connection NAME the call went to, as recorded on the call itself
   *  (`toolCalls[].connection`). `null` for a core control tool and for any
   *  record written before that field existed. */
  connection?: string | null;
  /** Ask for the present-participle form ("Looking up deals"). */
  running?: boolean;
}

/** Plugin data changes only when a plugin folder changes on disk, so this is
 *  cached hard -- the same reasoning `useMcpConnections` documents for the
 *  guardrail payload it fetches from the same manifests. */
export function useToolLabels(connection: string | null) {
  return useQuery({
    queryKey: ["toolLabels", connection ?? ""],
    queryFn: () =>
      api.get<ToolLabelCatalogueDTO>(
        `/mcp/connections/${encodeURIComponent(connection ?? "")}/tool-labels`,
      ),
    enabled: !!connection,
    staleTime: 10 * 60 * 1000,
  });
}

const PLACEHOLDER = /\{([a-zA-Z_][a-zA-Z0-9_]*)\}/g;

/** Fills `{name}` holes, or returns null if ANY hole has no value -- a label
 *  that would render a literal `{count}` at somebody must fall back to a
 *  duller sentence that is at least true. */
function fill(template: string, values: Record<string, string>): string | null {
  let failed = false;
  const out = template.replace(PLACEHOLDER, (_match, name: string) => {
    const value = values[name];
    if (value === undefined || value === "") {
      failed = true;
      return "";
    }
    return value;
  });
  if (failed) return null;
  const tidy = out.replace(/\s+/g, " ").trim();
  return tidy === "" ? null : tidy;
}

function placeholderValues(
  call: LabelledCall,
  catalogue: ToolLabelCatalogueDTO | undefined,
  lang: Lang,
): Record<string, string> {
  const values: Record<string, string> = {};
  for (const [key, raw] of Object.entries(call.arguments ?? {})) {
    if (typeof raw === "string" || typeof raw === "number" || typeof raw === "boolean") {
      values[key] = String(raw);
    }
  }
  if (call.connection) values.connection = call.connection;
  // `{model_label}`: whichever argument value the pack itself has a label
  // for. Core never learns which argument name carries an entity.
  for (const entry of catalogue?.modelLabels ?? []) {
    if (Object.values(values).includes(entry.key)) {
      values.model_label = resolveTranslation(entry.label, entry.labelTranslations, lang);
      break;
    }
  }
  return values;
}

/** Tier two: oc8's own control tools (agent/control_tools.py's
 *  CONTROL_TOOL_SCHEMAS). Core tools, so a core sentence is correct here --
 *  this is not a vendor translation table. Returns null for anything else. */
function controlToolLabel(tool: string, running: boolean, t: Translate): string | null {
  switch (tool) {
    case "memory_write":
      return running ? t("Making a note", "Notiert etwas") : t("Made a note", "Notiz gemacht");
    case "search_memory":
      return running
        ? t("Looking through its notes", "Sieht seine Notizen durch")
        : t("Looked through its notes", "Notizen durchgesehen");
    case "todo_write":
      return running
        ? t("Updating its plan", "Aktualisiert seinen Plan")
        : t("Updated its plan", "Plan aktualisiert");
    case "ask_user":
      return running ? t("Asking a question", "Stellt eine Frage") : t("Asked you", "Hat gefragt");
    case "delegate_task":
      return running
        ? t("Handing work to a colleague", "Gibt Arbeit an einen Kollegen")
        : t("Handed work to a colleague", "Arbeit an einen Kollegen gegeben");
    case "request_decision":
      return running
        ? t("Asking for a decision", "Bittet um eine Entscheidung")
        : t("Asked for a decision", "Um eine Entscheidung gebeten");
    case "search_knowledge":
      return running
        ? t("Searching the knowledge base", "Durchsucht die Wissensbasis")
        : t("Searched the knowledge base", "Wissensbasis durchsucht");
    case "fetch_url":
      return running
        ? t("Opening a web page", "Öffnet eine Webseite")
        : t("Read a web page", "Webseite gelesen");
    case "render_component":
      return running
        ? t("Preparing a summary", "Bereitet eine Auswertung vor")
        : t("Showed a summary", "Auswertung gezeigt");
    case "propose_change":
      return running
        ? t("Preparing a proposal", "Bereitet einen Vorschlag vor")
        : t("Proposed a change", "Änderung vorgeschlagen");
    case "decide_approval":
      return running
        ? t("Deciding a request", "Entscheidet eine Anfrage")
        : t("Decided a request", "Anfrage entschieden");
    case "list_pending_approvals":
      return running
        ? t("Checking what needs a decision", "Prüft, was zu entscheiden ist")
        : t("Checked what needs a decision", "Geprüft, was zu entscheiden ist");
    case "read_reference_file":
    case "read_instruction_file":
      return running
        ? t("Reading its instructions", "Liest seine Anweisungen")
        : t("Read its instructions", "Anweisungen gelesen");
    case "read_run_file":
      return running ? t("Opening a file", "Öffnet eine Datei") : t("Read a file", "Datei gelesen");
    case "write_output_file":
      return running
        ? t("Writing a file", "Schreibt eine Datei")
        : t("Wrote a file", "Datei geschrieben");
    case "department_status":
      return running
        ? t("Checking the department", "Prüft die Abteilung")
        : t("Checked the department", "Abteilung geprüft");
    case "agent_status":
      return running
        ? t("Checking a colleague", "Prüft einen Kollegen")
        : t("Checked a colleague", "Kollegen geprüft");
    case "budget_overview":
      return running
        ? t("Checking the budget", "Prüft das Budget")
        : t("Checked the budget", "Budget geprüft");
    case "kpi_overview":
      return running
        ? t("Checking the numbers", "Prüft die Zahlen")
        : t("Checked the numbers", "Zahlen geprüft");
    case "find_tools":
      return running
        ? t("Looking for a tool", "Sucht nach einem Werkzeug")
        : t("Looked for a tool", "Nach einem Werkzeug gesucht");
    case "read_resource":
      return running
        ? t("Reading a resource", "Liest eine Ressource")
        : t("Read a resource", "Ressource gelesen");
    case "procedure_step_done":
      return running
        ? t("Recording a completed step", "Erfasst einen erledigten Schritt")
        : t("Recorded a completed step", "Erledigten Schritt erfasst");
    case "run_shell":
      return running
        ? t("Running a command", "Führt einen Befehl aus")
        : t("Ran a command", "Befehl ausgeführt");
    case "run_program":
      return running
        ? t("Running a program", "Führt ein Programm aus")
        : t("Ran a program", "Programm ausgeführt");
    default:
      return null;
  }
}

export function resolveToolLabel(
  call: LabelledCall,
  catalogue: ToolLabelCatalogueDTO | undefined,
  t: Translate,
  lang: Lang,
): string {
  const declared = catalogue?.labels.find((entry) => entry.tool === call.tool);
  if (declared) {
    const values = placeholderValues(call, catalogue, lang);
    const template = call.running
      ? resolveTranslation(declared.running, declared.runningTranslations, lang)
      : [
          resolveTranslation(declared.verb, declared.verbTranslations, lang),
          declared.object
            ? resolveTranslation(declared.object, declared.objectTranslations, lang)
            : "",
        ]
          .filter(Boolean)
          .join(" ");
    const filled = template ? fill(template, values) : null;
    if (filled) return filled;
  }

  const control = controlToolLabel(call.tool, !!call.running, t);
  if (control) return control;

  const where = call.connection ?? "";
  if (where && catalogue) {
    if (catalogue.read.includes(call.tool)) {
      return call.running
        ? t(`Reading from ${where}`, `Liest aus ${where}`)
        : t(`Read from ${where}`, `In ${where} nachgesehen`);
    }
    if (catalogue.modify.includes(call.tool)) {
      return call.running
        ? t(`Changing something in ${where}`, `Ändert etwas in ${where}`)
        : t(`Changed something in ${where}`, `Etwas in ${where} geändert`);
    }
  }

  // Last resort, and only when even the connection's rights are unknown --
  // a run older than the `connection` field, or a sandbox toolset that has
  // no connection row to ask about.
  return call.tool || t("Did something", "Hat etwas getan");
}
