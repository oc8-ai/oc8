export type SkillCategory = "finance" | "sales" | "legal" | "operations" | "hr" | "support";

export type SkillOrigin = "local" | "store";

export interface Skill {
  id: string;
  name: string;
  description: string;
  /** Free-form: the backend accepts any string. `skillCategories` lists the
   * ones with a translated label; everything else is shown as it comes. */
  category: string;
  origin: SkillOrigin;
  version: string;
  author: string;
  tools: string[];
  knowledge?: string[];
  guardrails: string[];
  instructions: string;
  /** The version an assignment points at. The API has always sent it; the type
   * did not, so the assign button had nothing to send and faked success. */
  currentVersionId?: string | null;
  usedByAgents: number;
  /** Live assignments (any scope) across every version of the skill. */
  assignmentCount?: number;
  installs?: number;
  price?: "free" | number;
  updatedAt: string;
  /** Set once the skill is archived (`DELETE /skills/{id}` with dependents).
   * Optional because the backend does not send it yet — see Task 16's report. */
  deletedAt?: string | null;
  /** Set once an admin changed a shipped (store) skill's content in place. */
  customizedAt?: string | null;
  /** A newer shipped version arrived while the skill was customised; a reset
   * takes it. */
  shippedUpdateAvailable?: boolean;
  nameTranslations?: Record<string, string>;
  descriptionTranslations?: Record<string, string>;
  instructionsTranslations?: Record<string, string>;
  guardrailsTranslations?: Record<string, string[]>;
  /** The untranslated text of the current version, set by `localizeSkill` when it
   * swaps in a translation. Editors start from this, never from the display text. */
  source?: { name: string; description: string; instructions: string; guardrails: string[] };
}

export const skillCategories: { id: SkillCategory; label: string; labelDe: string }[] = [
  { id: "finance", label: "Finance", labelDe: "Finanzen" },
  { id: "sales", label: "Sales", labelDe: "Vertrieb" },
  { id: "legal", label: "Legal", labelDe: "Recht" },
  { id: "operations", label: "Operations", labelDe: "Betrieb" },
  { id: "hr", label: "HR", labelDe: "Personal" },
  { id: "support", label: "Support", labelDe: "Support" },
];

export const skills: Skill[] = [
  {
    id: "sk-invoice-check",
    name: "Invoice Review & Booking",
    description:
      "Validates incoming invoices against POs, extracts line items and books them to the correct GL account.",
    category: "finance",
    origin: "local",
    version: "1.4.2",
    author: "oc8 core",
    tools: ["Accounting", "Procurement", "OCR"],
    knowledge: ["Chart of Accounts", "Vendor Master"],
    guardrails: [
      "Amounts above €10 000 require human approval",
      "Reject invoices without valid VAT ID",
    ],
    instructions:
      "Given an invoice PDF, extract header + line items, match against open POs, book to the correct GL account and route to approval if thresholds apply.",
    usedByAgents: 4,
    updatedAt: "2026-06-28",
  },
  {
    id: "sk-lead-qualify",
    name: "Lead Qualification (BANT)",
    description: "Enriches inbound leads and scores them by BANT criteria before routing to AE.",
    category: "sales",
    origin: "local",
    version: "2.1.0",
    author: "Sales Ops",
    tools: ["CRM", "Data Enrichment", "Social Network"],
    knowledge: ["ICP Definition", "Sales Playbook"],
    guardrails: ["Never contact leads on the do-not-call list"],
    instructions:
      "Enrich the lead, evaluate Budget / Authority / Need / Timeline, produce a score 0-100 and route.",
    usedByAgents: 2,
    updatedAt: "2026-06-14",
  },
  {
    id: "sk-contract-summary",
    name: "Contract Summarisation",
    description: "Summarises contracts with obligations, terms, renewal dates and risk flags.",
    category: "legal",
    origin: "local",
    version: "1.0.7",
    author: "Legal Team",
    tools: ["E-Signature", "OCR"],
    knowledge: ["Legal Templates"],
    guardrails: ["Do not give legal advice", "Flag jurisdiction changes"],
    instructions:
      "Parse the contract, produce structured summary with parties, term, renewal, obligations and risks.",
    usedByAgents: 3,
    updatedAt: "2026-05-30",
  },
  {
    id: "sk-ticket-triage",
    name: "Support Ticket Triage",
    description: "Classifies inbound tickets, sets priority and drafts a first response.",
    category: "support",
    origin: "store",
    version: "3.2.1",
    author: "Community · helpdesk-labs",
    tools: ["Helpdesk", "Helpdesk"],
    knowledge: ["Help Center"],
    guardrails: ["Escalate legal or safety issues immediately"],
    instructions:
      "Classify by product area, set priority, propose response draft citing help-center articles.",
    usedByAgents: 1,
    installs: 4210,
    price: "free",
    updatedAt: "2026-07-01",
  },
  {
    id: "sk-onboarding",
    name: "New-Hire Onboarding",
    description: "Coordinates equipment, accounts and first-week schedule for new hires.",
    category: "hr",
    origin: "store",
    version: "1.1.0",
    author: "Community · people-ops",
    tools: ["HR System", "Chat", "Office Suite"],
    guardrails: ["Never share private employee data outside HR channel"],
    instructions:
      "Given a new-hire record, provision accounts, order equipment, schedule intro meetings.",
    usedByAgents: 0,
    installs: 892,
    price: 12,
    updatedAt: "2026-06-20",
  },
  {
    id: "sk-inventory-recon",
    name: "Inventory Reconciliation",
    description: "Reconciles physical stock against ERP and posts adjustment journals.",
    category: "operations",
    origin: "store",
    version: "0.9.4",
    author: "Community · ops-guild",
    tools: ["ERP", "Spreadsheets"],
    guardrails: ["Adjustments above 2% variance require approval"],
    instructions:
      "Compare cycle-count file with ERP quantities, produce variance report, post approved adjustments.",
    usedByAgents: 0,
    installs: 317,
    price: "free",
    updatedAt: "2026-05-11",
  },
];
