// The words around a member's own guardrails (D3a): the "what this agent may
// do for me" summary, the effect sentence under a row, the plain-language
// reading of "never … without me", and the action an approval card names.
// All deterministic templates through `t()` -- never the model's words.

import { LEVELS, type Level } from "@/lib/guardrail-levels";
import type { Approval } from "@/lib/hooks";
import type { MyToolGuardrail, SummaryLine } from "@/lib/hooks-member-guardrails";

type Translate = (en: string, de: string) => string;

function fill(template: string, values: Record<string, string>): string {
  return Object.entries(values).reduce(
    (out, [key, value]) => out.split(`{${key}}`).join(value),
    template,
  );
}

/** A tool key as a card names it: "gmail" -> "Gmail". */
export function toolLabel(key: string): string {
  return key ? key.charAt(0).toUpperCase() + key.slice(1) : key;
}

function joinTools(tools: string[], t: Translate): string {
  if (tools.length <= 1) return tools[0] ?? "";
  return `${tools.slice(0, -1).join(", ")} ${t("and", "und")} ${tools[tools.length - 1]}`;
}

/** How a sentence opens on the agent: a role ("Sales agent") reads "The sales
 * agent" / "Der Sales agent", a name ("Nora") stays a name. */
export function agentSubject(agentName: string, t: Translate): string {
  const isRole = /\s/.test(agentName.trim()) || /agent$/i.test(agentName.trim());
  if (!isRole) return agentName;
  return t(`The ${agentName.charAt(0).toLowerCase()}${agentName.slice(1)}`, `Der ${agentName}`);
}

function euro(value: number): string {
  return `€${value.toLocaleString()}`;
}

/** One sentence per summary code, in the order the backend sends them. */
export function summarySentences(
  summary: SummaryLine[],
  agentName: string,
  t: Translate,
): string[] {
  const subject = agentSubject(agentName, t);
  return summary.flatMap((line) => {
    const tools = joinTools(line.params.tools ?? [], t);
    const threshold = line.params.threshold;
    const values = { agent: subject, tools, threshold: threshold != null ? euro(threshold) : "" };
    switch (line.code) {
      case "may_modify":
        return [
          fill(
            t(
              "{agent} may change things in {tools}.",
              "{agent} darf in {tools} Änderungen vornehmen.",
            ),
            values,
          ),
        ];
      case "may_read":
        return [
          fill(
            t("{agent} may look things up in {tools}.", "{agent} darf in {tools} nachsehen."),
            values,
          ),
        ];
      case "asks_you":
        return [
          fill(
            threshold != null
              ? t(
                  "{agent} asks you before it changes anything in {tools} over {threshold}.",
                  "{agent} fragt dich, bevor in {tools} etwas über {threshold} geändert wird.",
                )
              : t(
                  "{agent} asks you before it changes anything in {tools}.",
                  "{agent} fragt dich, bevor in {tools} etwas geändert wird.",
                ),
            values,
          ),
        ];
      case "asks_department":
        return [
          fill(
            threshold != null
              ? t(
                  "Changes in {tools} over {threshold} need the department's approval.",
                  "Änderungen in {tools} über {threshold} brauchen die Freigabe der Abteilung.",
                )
              : t(
                  "Changes in {tools} need the department's approval.",
                  "Änderungen in {tools} brauchen die Freigabe der Abteilung.",
                ),
            values,
          ),
        ];
      case "off":
        return [
          fill(t("{agent} can't use {tools}.", "{agent} kann {tools} nicht nutzen."), values),
        ];
      default:
        return [];
    }
  });
}

/** The effect sentence under a member row, announced after a change. */
export function effectSentence(
  level: Level,
  toolKey: string,
  agentName: string,
  t: Translate,
): string {
  const values = { agent: agentSubject(agentName, t), tool: toolKey };
  switch (level) {
    case "off":
      return fill(t("{agent} can't use {tool}.", "{agent} kann {tool} nicht nutzen."), values);
    case "read_only":
      return fill(
        t("{agent} can only look things up in {tool}.", "{agent} kann in {tool} nur nachsehen."),
        values,
      );
    case "ask_first":
      return fill(
        t(
          "{agent} asks you before it changes anything in {tool}.",
          "{agent} fragt dich, bevor in {tool} etwas geändert wird.",
        ),
        values,
      );
    case "allowed":
      return fill(
        t(
          "{agent} may change things in {tool} on its own.",
          "{agent} darf in {tool} selbst Änderungen vornehmen.",
        ),
        values,
      );
  }
}

export interface InstructionReading {
  toolKey: string;
  level: Level;
}

const READ_ONLY = /\b(read|reading|look|looks|lesen|nachsehen|nur lesen|ansehen)\b/;
const OFF =
  /(never use|don't use|do not use|not at all|switch off|turn off|nie nutzen|nicht nutzen|nie benutzen|nicht benutzen|gar nicht|ausschalten|abschalten)/;
// Words long enough to name a tool by part ("mail" in "gmail") but not
// sentence glue that happens to sit inside a tool key.
const STOP = new Set([
  "never",
  "without",
  "always",
  "first",
  "ohne",
  "mich",
  "immer",
  "erst",
  "nichts",
  "nothing",
  "anything",
  "something",
  "etwas",
  "with",
  "from",
  "into",
  "über",
  "about",
]);

/** A plain reading of "never mail a customer without me": which tools the
 * text names (by key, or by a word of at least four letters inside the key),
 * and how strict -- "read" means Read only, "never use" means Off, anything
 * else Ask me first, the answer to "What should never happen without you?".
 * A tool whose department or agent is already at least as strict is left out:
 * there is nothing to tighten. `[]` means "not understood". */
export function readInstruction(text: string, tools: MyToolGuardrail[]): InstructionReading[] {
  const lower = text.toLowerCase();
  const words = lower.split(/[^\p{L}\p{N}_-]+/u).filter((w) => w.length >= 4 && !STOP.has(w));
  const level: Level = OFF.test(lower) ? "off" : READ_ONLY.test(lower) ? "read_only" : "ask_first";
  return tools
    .filter((tool) => {
      const key = tool.toolKey.toLowerCase();
      return lower.includes(key) || words.some((w) => key.includes(w));
    })
    .filter((tool) => LEVELS.indexOf(level) < LEVELS.indexOf(tool.level))
    .map((tool) => ({ toolKey: tool.toolKey, level }));
}

/** Two examples for the "not understood" state, with the member's own tools. */
export function instructionExamples(tools: MyToolGuardrail[], t: Translate): string[] {
  const first = tools[0]?.toolKey ?? "gmail";
  const second = tools[1]?.toolKey ?? first;
  return [
    fill(t("Never change anything in {tool} without me", "Nie etwas in {tool} ändern ohne mich"), {
      tool: first,
    }),
    fill(t("Only let it read {tool}", "In {tool} nur lesen lassen"), { tool: second }),
  ];
}

const RECIPIENT_KEYS = ["to", "recipient", "recipients", "email", "email_to", "partner_email"];

/** What a held call does, in a few words: "send mail to kunde@x.de". */
export function approvalActionText(approval: Approval, t: Translate): string {
  const tool = approval.toolName?.replace(/[_.-]+/g, " ").trim();
  if (!tool) return approval.title;
  const args = approval.toolArguments ?? {};
  for (const key of RECIPIENT_KEYS) {
    const raw = args[key];
    const value = Array.isArray(raw) ? raw.filter((v) => typeof v === "string").join(", ") : raw;
    if (typeof value === "string" && value) return `${tool} ${t("to", "an")} ${value}`;
  }
  return tool;
}
