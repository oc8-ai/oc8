export type AgentStatus = "running" | "warning" | "error" | "paused" | "waiting_for_task";

export interface Agent {
  id: string;
  name: string;
  role: string;
  llm: string;
  provider: "Claude" | "GPT" | "Mistral" | "Ollama";
  status: AgentStatus;
  tools: string[];
  lastAction: string;
  lastRun: string;
  tasksToday: number;
  guardrails: string[];
  schedule: string;
  avatarColor: string;
  departmentId?: string;
  isLead?: boolean;
  /**
   * Team lead ("Teamleiter") supervising this agent. Reference to another
   * agent id, or `"human"` for human oversight. If omitted the department
   * lead is used as an implicit supervisor.
   */
  supervisorId?: string;
  /** Set once the agent is archived (`DELETE /agents/{id}` with dependents).
   * Mirrors `Skill.deletedAt` -- see Task 16's report. */
  deletedAt?: string | null;
  /** Demo-seed locale overlays. Empty on live agents. */
  roleTranslations?: Record<string, string>;
  lastActionTranslations?: Record<string, string>;
  lastRunTranslations?: Record<string, string>;
  scheduleTranslations?: Record<string, string>;
  missionTranslations?: Record<string, string>;
  guardrailsTranslations?: Record<string, string[]>;
}

export const agents: Agent[] = [
  {
    id: "vera",
    name: "Vera",
    role: "Sales Assistant",
    llm: "Claude 3.5 Sonnet",
    provider: "Claude",
    status: "warning",
    tools: ["CRM", "Office", "Email", "Calendar"],
    lastAction: "Prepared quote for client Bauer GmbH — awaiting approval",
    lastRun: "2 min ago",
    tasksToday: 24,
    guardrails: ["Max quote value €10,000", "No discounts >15%", "Approval required from €5,000"],
    schedule: "Mon–Fri, 08:00–18:00",
    avatarColor: "oklch(0.75 0.15 30)",
    departmentId: "vertrieb",
    isLead: true,
  },
  {
    id: "data",
    name: "Data",
    role: "Data Analysis",
    llm: "GPT-4o",
    provider: "GPT",
    status: "running",
    tools: ["Data Warehouse", "Spreadsheets", "Chat"],
    lastAction: "Generated weekly revenue report and shared to #sales",
    lastRun: "34 sec ago",
    tasksToday: 41,
    guardrails: ["Read-only database access", "No PII in reports"],
    schedule: "Continuous",
    avatarColor: "oklch(0.70 0.15 220)",
    departmentId: "marketing",
  },
  {
    id: "doku",
    name: "Doku",
    role: "Internal Documentation",
    llm: "Mistral Large",
    provider: "Mistral",
    status: "running",
    tools: ["Wiki", "Wiki", "Code Host"],
    lastAction: "Summarized changes to API documentation",
    lastRun: "7 min ago",
    tasksToday: 18,
    guardrails: ["Internal wikis only", "No code changes"],
    schedule: "On-Demand",
    avatarColor: "oklch(0.70 0.14 155)",
    departmentId: "entwicklung",
  },
  {
    id: "hera",
    name: "Hera",
    role: "HR Assistant",
    llm: "Llama 3.1 (local)",
    provider: "Ollama",
    status: "paused",
    tools: ["HR System", "Office", "Email"],
    lastAction: "Paused by admin — GDPR review",
    lastRun: "2 hrs ago",
    tasksToday: 6,
    guardrails: ["Local LLM only", "No external APIs", "Employee data encrypted"],
    schedule: "Mon–Fri, 09:00–17:00",
    avatarColor: "oklch(0.72 0.14 320)",
    departmentId: "hr",
    isLead: true,
  },
  {
    id: "fin",
    name: "Fin",
    role: "Accounting",
    llm: "Claude 3.5 Sonnet",
    provider: "Claude",
    status: "running",
    tools: ["Accounting", "ERP", "Email"],
    lastAction: "Categorized 12 incoming invoices and transferred to the accounting system",
    lastRun: "12 min ago",
    tasksToday: 37,
    guardrails: ["Approval for bookings >€2,500", "Four-eyes principle active"],
    schedule: "Weekdays, 07:00–20:00",
    avatarColor: "oklch(0.75 0.15 85)",
    departmentId: "buchhaltung",
    isLead: true,
  },
  {
    id: "ops",
    name: "Ops",
    role: "IT Monitoring",
    llm: "GPT-4o mini",
    provider: "GPT",
    status: "error",
    tools: ["Monitoring", "On-call", "Code Host", "Chat"],
    lastAction: "Error: Connection to the monitoring API dropped (401)",
    lastRun: "4 min ago",
    tasksToday: 16,
    guardrails: ["No production deploys", "Create alerts only"],
    schedule: "24/7",
    avatarColor: "oklch(0.68 0.20 25)",
    departmentId: "support",
  },
  // Sales
  {
    id: "leo",
    name: "Leo",
    role: "Outbound Sales",
    llm: "GPT-4o mini",
    provider: "GPT",
    status: "running",
    tools: ["CRM", "Social Network", "Email"],
    lastAction: "Sent 24 cold emails to 'Manufacturing DACH' segment",
    lastRun: "1 min ago",
    tasksToday: 28,
    guardrails: ["Max 50 emails/day", "B2B addresses only"],
    schedule: "Mon–Fri, 08:00–17:00",
    avatarColor: "oklch(0.72 0.14 45)",
    departmentId: "vertrieb",
  },
  {
    id: "nina",
    name: "Nina",
    role: "Lead Qualification",
    llm: "Claude 3.5 Haiku",
    provider: "Claude",
    status: "running",
    tools: ["CRM", "Chat"],
    lastAction: "Scored 9 new leads — 3 marked as 'Hot'",
    lastRun: "3 min ago",
    tasksToday: 21,
    guardrails: ["Scoring only, no customer contact"],
    schedule: "Mon–Fri, 09:00–18:00",
    avatarColor: "oklch(0.74 0.14 15)",
    departmentId: "vertrieb",
  },
  // Engineering
  {
    id: "dex",
    name: "Dex",
    role: "Engineering Lead",
    llm: "Claude 3.5 Sonnet",
    provider: "Claude",
    status: "running",
    tools: ["Code Host", "Issue Tracker", "Chat"],
    lastAction: "Completed PR review for #482 — 2 comments",
    lastRun: "40 sec ago",
    tasksToday: 34,
    guardrails: ["No force-pushes to main", "Approval for prod deploys"],
    schedule: "Mon–Fri, 09:00–19:00",
    avatarColor: "oklch(0.72 0.14 250)",
    departmentId: "entwicklung",
    isLead: true,
  },
  {
    id: "ada",
    name: "Ada",
    role: "Backend Development",
    llm: "GPT-4o",
    provider: "GPT",
    status: "running",
    tools: ["Code Host", "Issue Tracker"],
    lastAction: "Implemented 'Batch Export' feature — tests green",
    lastRun: "6 min ago",
    tasksToday: 19,
    guardrails: ["Feature branches only", "Coverage >80%"],
    schedule: "Continuous",
    avatarColor: "oklch(0.74 0.15 200)",
    departmentId: "entwicklung",
  },
  {
    id: "kern",
    name: "Kern",
    role: "QA & Tests",
    llm: "Mistral Large",
    provider: "Mistral",
    status: "warning",
    tools: ["Code Host", "Test Runner", "Chat"],
    lastAction: "E2E test run: 2 regressions found — awaiting triage",
    lastRun: "8 min ago",
    tasksToday: 12,
    guardrails: ["No prod database access"],
    schedule: "Continuous",
    avatarColor: "oklch(0.72 0.14 130)",
    departmentId: "entwicklung",
  },
  // Marketing
  {
    id: "mara",
    name: "Mara",
    role: "Marketing Lead",
    llm: "Claude 3.5 Sonnet",
    provider: "Claude",
    status: "running",
    tools: ["CRM", "Social Network", "Ads Platform"],
    lastAction: "Rolled out 'Q3 Launch' campaign on the social network",
    lastRun: "5 min ago",
    tasksToday: 15,
    guardrails: ["Budget cap €2,000/week", "Brand guidelines"],
    schedule: "Mon–Fri, 08:00–18:00",
    avatarColor: "oklch(0.75 0.15 340)",
    departmentId: "marketing",
    isLead: true,
  },
  {
    id: "tim",
    name: "Tim",
    role: "Content Editor",
    llm: "GPT-4o",
    provider: "GPT",
    status: "running",
    tools: ["Wiki", "CMS"],
    lastAction: "Blog post 'Agents in Practice' in review",
    lastRun: "11 min ago",
    tasksToday: 8,
    guardrails: ["No auto-publishing"],
    schedule: "Mon–Fri, 09:00–17:00",
    avatarColor: "oklch(0.74 0.15 300)",
    departmentId: "marketing",
  },
  // Accounting
  {
    id: "cent",
    name: "Cent",
    role: "Travel Expenses & Receipts",
    llm: "Mistral Large",
    provider: "Mistral",
    status: "running",
    tools: ["Accounting", "Office"],
    lastAction: "OCR-processed and reviewed 18 travel receipts",
    lastRun: "15 min ago",
    tasksToday: 22,
    guardrails: ["Max single receipt €500 without approval"],
    schedule: "Weekdays, 08:00–17:00",
    avatarColor: "oklch(0.75 0.15 70)",
    departmentId: "buchhaltung",
  },
  // Support
  {
    id: "sam",
    name: "Sam",
    role: "Support Lead",
    llm: "Claude 3.5 Sonnet",
    provider: "Claude",
    status: "running",
    tools: ["Helpdesk", "Chat", "Wiki"],
    lastAction: "Resolved ticket #4412 — SSO connection issue",
    lastRun: "2 min ago",
    tasksToday: 31,
    guardrails: ["No refunds without approval", "PII filter"],
    schedule: "Mon–Sat, 08:00–20:00",
    avatarColor: "oklch(0.74 0.15 175)",
    departmentId: "support",
    isLead: true,
  },
  {
    id: "echo",
    name: "Echo",
    role: "First-Level Support",
    llm: "GPT-4o mini",
    provider: "GPT",
    status: "running",
    tools: ["Helpdesk", "Wiki"],
    lastAction: "Generated 14 answers from knowledge base",
    lastRun: "45 sec ago",
    tasksToday: 47,
    guardrails: ["Standard replies only", "Escalate from level 2"],
    schedule: "24/7",
    avatarColor: "oklch(0.74 0.15 155)",
    departmentId: "support",
  },
];

export interface Escalation {
  id: string;
  agentId: string;
  title: string;
  detail: string;
  amount?: string;
  time: string;
}

export const escalations: Escalation[] = [
  {
    id: "e1",
    agentId: "vera",
    title: "Vera wants to send a quote",
    detail: "Bauer GmbH — 2026 maintenance contract, incl. remote-service add-on.",
    amount: "€7,400",
    time: "2 min ago",
  },
  {
    id: "e2",
    agentId: "fin",
    title: "Fin wants to approve booking",
    detail: "Vendor Meier & Co. — invoice R-2026-0331, unusually high amount.",
    amount: "€3,280",
    time: "9 min ago",
  },
  {
    id: "e3",
    agentId: "vera",
    title: "Vera wants to grant a discount",
    detail: "Client Nordheim AG requests a 12% discount on annual contract.",
    amount: "-€4,320",
    time: "21 min ago",
  },
];

export interface ActivityItem {
  id: string;
  agentId: string;
  status: "success" | "warning" | "error" | "info";
  message: string;
  time: string;
  detail?: string;
  cacheHit?: boolean;
  messageTranslations?: Record<string, string>;
  detailTranslations?: Record<string, string>;
}

export const activity: ActivityItem[] = [
  {
    id: "a1",
    agentId: "data",
    status: "success",
    message: "Sent revenue report W27",
    time: "14:42",
    detail: "Recipients: #sales, cfo@acme.io. Attachment: revenue-w27.pdf",
  },
  {
    id: "a2",
    agentId: "vera",
    status: "warning",
    message: "Approval requested: Bauer GmbH quote (€7,400)",
    time: "14:38",
  },
  {
    id: "a3",
    agentId: "fin",
    status: "success",
    message: "Transferred 12 incoming invoices to the accounting system",
    time: "14:30",
    detail: "Batch ID ACC-B-8391 · booking account 3400",
  },
  {
    id: "a4",
    agentId: "ops",
    status: "error",
    message: "Monitoring API returns 401 — check API key",
    time: "14:22",
  },
  {
    id: "a5",
    agentId: "doku",
    status: "success",
    message: "Generated changelog from 8 pull requests",
    time: "14:11",
  },
  {
    id: "a6",
    agentId: "vera",
    status: "info",
    message: "Scanned new lead email from info@holtmann.de",
    time: "13:57",
  },
  {
    id: "a7",
    agentId: "data",
    status: "success",
    message: "Updated 'Pipeline Health' dashboard",
    time: "13:42",
  },
  {
    id: "a8",
    agentId: "hera",
    status: "info",
    message: "Agent paused by admin@oc8.io",
    time: "12:04",
  },
  {
    id: "a9",
    agentId: "fin",
    status: "warning",
    message: "Approval requested: R-2026-0331 (€3,280)",
    time: "11:51",
  },
  {
    id: "a10",
    agentId: "doku",
    status: "success",
    message: "Bundled onboarding wiki for Product X",
    time: "11:12",
  },
];

export interface Integration {
  id: string;
  name: string;
  category: string;
  connected: boolean;
  usedBy?: string[];
  desc: string;
  hue: number;
}

export const integrations: Integration[] = [
  {
    id: "erp",
    name: "ERP",
    category: "ERP",
    connected: true,
    usedBy: ["fin"],
    desc: "Invoices, orders, accounting.",
    hue: 280,
  },
  {
    id: "crm",
    name: "CRM",
    category: "CRM",
    connected: true,
    usedBy: ["vera"],
    desc: "Contacts, deals, pipeline.",
    hue: 25,
  },
  {
    id: "office",
    name: "Office Suite",
    category: "Productivity",
    connected: true,
    usedBy: ["vera", "hera", "fin"],
    desc: "Mail, calendar, chat, document store.",
    hue: 220,
  },
  {
    id: "chat",
    name: "Chat",
    category: "Communication",
    connected: true,
    usedBy: ["data", "ops"],
    desc: "Channels, messages, alerts.",
    hue: 320,
  },
  {
    id: "code-host",
    name: "Code Host",
    category: "Engineering",
    connected: true,
    usedBy: ["doku", "ops"],
    desc: "Repos, pull requests, issues.",
    hue: 250,
  },
  {
    id: "files",
    name: "Files",
    category: "Files",
    connected: false,
    desc: "Docs, sheets, folders.",
    hue: 155,
  },
  {
    id: "hr-system",
    name: "HR System",
    category: "HR",
    connected: false,
    desc: "Employees, time tracking, leave.",
    hue: 200,
  },
  {
    id: "accounting",
    name: "Accounting",
    category: "Accounting",
    connected: true,
    usedBy: ["fin"],
    desc: "Receipt import, chart of accounts.",
    hue: 40,
  },
  {
    id: "wiki",
    name: "Wiki",
    category: "Knowledge",
    connected: false,
    desc: "Wikis, notes, databases.",
    hue: 0,
  },
];

export interface KnowledgeEntry {
  id: string;
  title: string;
  category: string;
  updated: string;
  snippet: string;
}

export const sharedKnowledge: KnowledgeEntry[] = [
  {
    id: "k1",
    title: "2026 Price List",
    category: "Sales",
    updated: "3 days ago",
    snippet: "Base license €1,200/yr, Enterprise €8,400/yr, tiered discount from 25 seats...",
  },
  {
    id: "k2",
    title: "Approval Policy",
    category: "Compliance",
    updated: "1 week ago",
    snippet: "Quotes >€5,000 approved by Sales Lead, bookings >€2,500 approved by CFO.",
  },
  {
    id: "k3",
    title: "Tone of Voice",
    category: "Communication",
    updated: "2 weeks ago",
    snippet: "Factual, direct, no superlatives. Informal internally, formal with customers.",
  },
  {
    id: "k4",
    title: "Product Roadmap Q3",
    category: "Product",
    updated: "5 days ago",
    snippet: "Focus: MCP integrations, Guardrails 2.0, team roles & approval workflows.",
  },
  {
    id: "k5",
    title: "GDPR Guide",
    category: "Compliance",
    updated: "1 month ago",
    snippet: "Process personal data in EU region only. Prefer local LLMs for HR.",
  },
];

export const privateMemory: Record<string, KnowledgeEntry[]> = {
  vera: [
    {
      id: "vp1",
      title: "Client Bauer GmbH",
      category: "Account notes",
      updated: "today",
      snippet: "Contact Mr. Menke, prefers calls in the morning.",
    },
    {
      id: "vp2",
      title: "Objection library",
      category: "Playbook",
      updated: "6 days ago",
      snippet: "'Too expensive' → show ROI for module C.",
    },
  ],
  data: [
    {
      id: "dp1",
      title: "Report templates",
      category: "SQL",
      updated: "2 days ago",
      snippet: "Weekly Sales, MRR movement, Pipeline Health.",
    },
  ],
  fin: [
    {
      id: "fp1",
      title: "Chart of accounts mapping",
      category: "Booking",
      updated: "4 days ago",
      snippet: "Software subs → 6835, cloud infra → 6836.",
    },
  ],
  doku: [
    {
      id: "dop1",
      title: "Style guide wiki",
      category: "Editorial",
      updated: "1 week ago",
      snippet: "H1 once per page, annotate code blocks with language.",
    },
  ],
};

export interface Model {
  id: string;
  provider: "Claude" | "GPT" | "Mistral" | "Ollama";
  name: string;
  status: "healthy" | "degraded" | "offline";
  costTier: "$" | "$$" | "$$$";
  latency: string;
  assignedTo: string[];
  note: string;
}

export const models: Model[] = [
  {
    id: "m1",
    provider: "Claude",
    name: "Claude 3.5 Sonnet",
    status: "healthy",
    costTier: "$$",
    latency: "1.2s",
    assignedTo: ["vera", "fin"],
    note: "Preferred for text & reasoning.",
  },
  {
    id: "m2",
    provider: "GPT",
    name: "GPT-4o",
    status: "healthy",
    costTier: "$$$",
    latency: "0.9s",
    assignedTo: ["data"],
    note: "Multimodal, Tool-Use.",
  },
  {
    id: "m3",
    provider: "GPT",
    name: "GPT-4o mini",
    status: "degraded",
    costTier: "$",
    latency: "2.4s",
    assignedTo: ["ops"],
    note: "Higher latency reported since 09:00.",
  },
  {
    id: "m4",
    provider: "Mistral",
    name: "Mistral Large",
    status: "healthy",
    costTier: "$",
    latency: "1.0s",
    assignedTo: ["doku"],
    note: "EU region, low cost.",
  },
  {
    id: "m5",
    provider: "Ollama",
    name: "Llama 3.1 8B (local)",
    status: "healthy",
    costTier: "$",
    latency: "1.8s",
    assignedTo: ["hera"],
    note: "On-premise, no data leaves the VPC.",
  },
];

export function agentById(id: string) {
  return agents.find((a) => a.id === id);
}

export const statusMeta: Record<AgentStatus, { label: string; className: string; dot: string }> = {
  running: {
    label: "running",
    className: "text-[color:var(--status-running)]",
    dot: "bg-[color:var(--status-running)]",
  },
  warning: {
    label: "awaiting approval",
    className: "text-[color:var(--status-warning)]",
    dot: "bg-[color:var(--status-warning)]",
  },
  error: {
    label: "error",
    className: "text-[color:var(--status-error)]",
    dot: "bg-[color:var(--status-error)]",
  },
  paused: {
    label: "paused",
    className: "text-[color:var(--status-paused)]",
    dot: "bg-[color:var(--status-paused)]",
  },
  waiting_for_task: {
    label: "waiting for task",
    className: "text-[color:var(--status-waiting-for-task)]",
    dot: "bg-[color:var(--status-waiting-for-task)]",
  },
};

// ============= Departments =============

export interface Department {
  id: string;
  name: string;
  icon: string;
  goal: string;
  okr: string;
  kpiLabel: string;
  kpiValue: string;
  activity: number; // 0..100
  accent: string; // hue for tinting
  promptCachingEnabled: boolean;
  /** Set once the department is archived (`DELETE /departments/{id}`).
   * Mirrors `Agent.deletedAt` -- see Task 18's report. */
  deletedAt?: string | null;
  nameTranslations?: Record<string, string>;
  goalTranslations?: Record<string, string>;
  okrTranslations?: Record<string, string>;
  kpiLabelTranslations?: Record<string, string>;
}

export const departments: Department[] = [
  {
    id: "vertrieb",
    name: "Sales",
    icon: "sales",
    goal: "Fill the pipeline",
    okr: "+30% qualified leads by end of Q3",
    kpiLabel: "Leads today",
    kpiValue: "18",
    activity: 82,
    accent: "oklch(0.75 0.15 30)",
    promptCachingEnabled: true,
  },
  {
    id: "entwicklung",
    name: "Engineering",
    icon: "engineering",
    goal: "Ship the feature backlog",
    okr: "12 story points / sprint · release 2.4",
    kpiLabel: "PRs in review",
    kpiValue: "3",
    activity: 74,
    accent: "oklch(0.72 0.14 250)",
    promptCachingEnabled: true,
  },
  {
    id: "marketing",
    name: "Marketing",
    icon: "marketing",
    goal: "Campaigns & content",
    okr: "2 campaigns live, CAC < €180",
    kpiLabel: "Campaigns live",
    kpiValue: "2",
    activity: 61,
    accent: "oklch(0.75 0.15 340)",
    promptCachingEnabled: true,
  },
  {
    id: "buchhaltung",
    name: "Accounting",
    icon: "finance",
    goal: "Invoices & reports",
    okr: "Monthly close by BD+3",
    kpiLabel: "Receipts processed",
    kpiValue: "37",
    activity: 68,
    accent: "oklch(0.75 0.15 85)",
    promptCachingEnabled: true,
  },
  {
    id: "hr",
    name: "People",
    icon: "hr",
    goal: "Review applications",
    okr: "Time-to-interview < 5 days",
    kpiLabel: "CVs in screening",
    kpiValue: "12",
    activity: 22,
    accent: "oklch(0.72 0.14 320)",
    promptCachingEnabled: true,
  },
  {
    id: "support",
    name: "Support",
    icon: "support",
    goal: "Resolve tickets",
    okr: "First response < 15 min, CSAT > 4.5",
    kpiLabel: "Tickets closed",
    kpiValue: "24",
    activity: 88,
    accent: "oklch(0.74 0.15 175)",
    promptCachingEnabled: true,
  },
];

export function departmentById(id: string) {
  return departments.find((d) => d.id === id);
}

export function agentsInDepartment(id: string) {
  return agents.filter((a) => a.departmentId === id);
}

export function leadOfDepartment(id: string) {
  return agents.find((a) => a.departmentId === id && a.isLead);
}

export type Supervisor =
  | { kind: "agent"; agent: Agent; source: "explicit" | "department-lead" }
  | { kind: "human"; source: "explicit" | "fallback" };

/**
 * Resolve the effective team lead ("Teamleiter") for an agent.
 * Precedence:
 *  1. explicit `agent.supervisorId` ("human" or another agent id)
 *  2. department lead — if different from the agent itself
 *  3. human oversight fallback
 */
export function supervisorOf(agent: Agent): Supervisor {
  if (agent.supervisorId === "human") return { kind: "human", source: "explicit" };
  if (agent.supervisorId) {
    const a = agents.find((x) => x.id === agent.supervisorId);
    if (a) return { kind: "agent", agent: a, source: "explicit" };
  }
  if (agent.departmentId) {
    const lead = leadOfDepartment(agent.departmentId);
    if (lead && lead.id !== agent.id) {
      return { kind: "agent", agent: lead, source: "department-lead" };
    }
  }
  return { kind: "human", source: "fallback" };
}

/**
 * Candidate supervisors for an agent: all department leads + other agents
 * from the same department. Excludes the agent themselves.
 */
export function supervisorCandidates(agent: Agent): Agent[] {
  const seen = new Set<string>();
  const out: Agent[] = [];
  for (const a of agents) {
    if (a.id === agent.id) continue;
    const sameDept = a.departmentId && a.departmentId === agent.departmentId;
    if (a.isLead || sameDept) {
      if (!seen.has(a.id)) {
        seen.add(a.id);
        out.push(a);
      }
    }
  }
  return out;
}

/**
 * True when this agent is referenced as a supervisor by at least one other
 * agent — either explicitly via `supervisorId`, or implicitly as the lead of
 * their department. Used to render the "team lead" crown across views.
 */
export function isTeamLead(agentId: string): boolean {
  return agents.some((a) => {
    if (a.id === agentId) return false;
    const sup = supervisorOf(a);
    return sup.kind === "agent" && sup.agent.id === agentId;
  });
}

// ============= Tasks (Kanban) =============

export type TaskColumn = "backlog" | "in_progress" | "waiting" | "done";

export interface Task {
  id: string;
  departmentId: string;
  title: string;
  agentId: string | null;
  column: TaskColumn;
  meta?: string;
  titleTranslations?: Record<string, string>;
  metaTranslations?: Record<string, string>;
}

export const tasks: Task[] = [
  // Sales
  {
    id: "t-v1",
    departmentId: "vertrieb",
    title: "Quote Bauer GmbH (€7,400)",
    agentId: "vera",
    column: "waiting",
    meta: "Approval > €5,000",
  },
  {
    id: "t-v2",
    departmentId: "vertrieb",
    title: "Follow-up Nordheim AG",
    agentId: "vera",
    column: "in_progress",
  },
  {
    id: "t-v3",
    departmentId: "vertrieb",
    title: "50 cold emails 'Manufacturing'",
    agentId: "leo",
    column: "in_progress",
  },
  {
    id: "t-v4",
    departmentId: "vertrieb",
    title: "Lead scoring W28",
    agentId: "nina",
    column: "in_progress",
  },
  {
    id: "t-v5",
    departmentId: "vertrieb",
    title: "Prepare discovery call",
    agentId: "vera",
    column: "backlog",
  },
  {
    id: "t-v6",
    departmentId: "vertrieb",
    title: "Import contacts from the CRM",
    agentId: "leo",
    column: "backlog",
  },
  {
    id: "t-v7",
    departmentId: "vertrieb",
    title: "Qualified 9 leads",
    agentId: "nina",
    column: "done",
  },
  {
    id: "t-v8",
    departmentId: "vertrieb",
    title: "Sent quote to Meier AG",
    agentId: "vera",
    column: "done",
  },

  // Engineering
  {
    id: "t-e1",
    departmentId: "entwicklung",
    title: "PR #482: Batch Export",
    agentId: "ada",
    column: "waiting",
    meta: "Review by Dex",
  },
  {
    id: "t-e2",
    departmentId: "entwicklung",
    title: "Triage regressions",
    agentId: "kern",
    column: "waiting",
    meta: "2 failures",
  },
  {
    id: "t-e3",
    departmentId: "entwicklung",
    title: "Refactor SDK v3",
    agentId: "dex",
    column: "in_progress",
  },
  {
    id: "t-e4",
    departmentId: "entwicklung",
    title: "Update API v3 docs",
    agentId: "doku",
    column: "in_progress",
  },
  {
    id: "t-e5",
    departmentId: "entwicklung",
    title: "Extend E2E suite",
    agentId: "kern",
    column: "backlog",
  },
  {
    id: "t-e6",
    departmentId: "entwicklung",
    title: "Bug #911: Session timeout",
    agentId: "ada",
    column: "backlog",
  },
  {
    id: "t-e7",
    departmentId: "entwicklung",
    title: "PR #479 merged",
    agentId: "dex",
    column: "done",
  },

  // Marketing
  {
    id: "t-m1",
    departmentId: "marketing",
    title: "Blog post 'Agents in practice'",
    agentId: "tim",
    column: "waiting",
    meta: "Publish approval",
  },
  {
    id: "t-m2",
    departmentId: "marketing",
    title: "Social campaign Q3",
    agentId: "mara",
    column: "in_progress",
  },
  {
    id: "t-m3",
    departmentId: "marketing",
    title: "Ads analysis",
    agentId: "data",
    column: "in_progress",
  },
  {
    id: "t-m4",
    departmentId: "marketing",
    title: "Draft newsletter W29",
    agentId: "tim",
    column: "backlog",
  },
  {
    id: "t-m5",
    departmentId: "marketing",
    title: "Campaign 'Spring' completed",
    agentId: "mara",
    column: "done",
  },

  // Accounting
  {
    id: "t-b1",
    departmentId: "buchhaltung",
    title: "Vendor Meier & Co. R-0331",
    agentId: "fin",
    column: "waiting",
    meta: "€3,280",
  },
  {
    id: "t-b2",
    departmentId: "buchhaltung",
    title: "Incoming invoices W28",
    agentId: "fin",
    column: "in_progress",
  },
  {
    id: "t-b3",
    departmentId: "buchhaltung",
    title: "Travel expenses sales team",
    agentId: "cent",
    column: "in_progress",
  },
  {
    id: "t-b4",
    departmentId: "buchhaltung",
    title: "VAT return July",
    agentId: "fin",
    column: "backlog",
  },
  {
    id: "t-b5",
    departmentId: "buchhaltung",
    title: "Booked 12 receipts",
    agentId: "cent",
    column: "done",
  },

  // HR
  {
    id: "t-h1",
    departmentId: "hr",
    title: "Screen 12 'Backend' CVs",
    agentId: "hera",
    column: "backlog",
  },
  {
    id: "t-h2",
    departmentId: "hr",
    title: "Wait for GDPR review",
    agentId: "hera",
    column: "waiting",
    meta: "Admin pause",
  },

  // Support
  {
    id: "t-s1",
    departmentId: "support",
    title: "Ticket #4498 refund",
    agentId: "sam",
    column: "waiting",
    meta: "Approval > €200",
  },
  {
    id: "t-s2",
    departmentId: "support",
    title: "Investigate SSO outage",
    agentId: "ops",
    column: "in_progress",
    meta: "Incident PROD-4412",
  },
  {
    id: "t-s3",
    departmentId: "support",
    title: "14 first-level tickets",
    agentId: "echo",
    column: "in_progress",
  },
  {
    id: "t-s4",
    departmentId: "support",
    title: "Update knowledge base",
    agentId: "sam",
    column: "backlog",
  },
  {
    id: "t-s5",
    departmentId: "support",
    title: "Resolved ticket #4412",
    agentId: "sam",
    column: "done",
  },
];

// ============= Knowledge (RAG) =============

export type SensitivityLevel = "public" | "internal" | "confidential" | "restricted";

export const sensitivityMeta: Record<SensitivityLevel, { label: string; color: string }> = {
  public: { label: "Public", color: "oklch(0.72 0.14 155)" },
  internal: { label: "Internal", color: "oklch(0.75 0.10 240)" },
  confidential: { label: "Confidential", color: "oklch(0.75 0.16 60)" },
  restricted: { label: "Restricted", color: "oklch(0.68 0.22 25)" },
};

// A connector type_id. website/upload are core; everything else (s3, gdrive, …)
// is contributed by an enabled connector plugin and labelled from the live
// /knowledge/connectors catalog, never from a static vendor map here.
export type DataSourceKind = string;

export interface DataSource {
  id: string;
  kind: DataSourceKind;
  name: string;
  connected: boolean;
  lastSync?: string;
  docCount?: number;
  schedule?: "hourly" | "daily" | "manual";
  sensitivity?: SensitivityLevel;
  scope?: string; // folder / space / URL
  // Non-secret connector config only -- a `credential`-typed key holds a
  // credential id, never a resolved secret value. Populated from the API's
  // DataSourceDTO.config; absent on locally-constructed/mock rows.
  config?: Record<string, unknown>;
  /** `ok` | `failed` | undefined (never synced yet). Separate from
   * `connected` (transport/credentials reachable) -- a failed sync does NOT
   * move `connected`, so this is the only field that tells a red sync
   * failure apart from a healthy, still-reachable connection. */
  lastSyncStatus?: "ok" | "failed";
  /** Set only while `lastSyncStatus === "failed"`. */
  lastSyncError?: string | null;
  /** Set once the source is deleted (`DELETE /knowledge/sources/{id}`) -- this
   * is IRREVERSIBLE (it reduces the source's `KbChunk`s in the same
   * transaction, §12.5.1), so unlike Skill/Agent/Department there is no
   * restore for this field; it only drives a read-only "Deleted" badge. See
   * the Design System Consistency plan's DataSource/KnowledgeBase archive
   * exception and Task 20's report. */
  deletedAt?: string | null;
}

// Display meta for the CORE connectors only. Plugin connectors are labelled
// from the live /knowledge/connectors catalog (their own self-declared label),
// so no vendor name is hard-coded here.
export const dataSourceKindMeta: Record<
  string,
  { label: string; hue: number; description: string }
> = {
  website: {
    label: "Website (crawler)",
    hue: 285,
    description: "Crawl public or intranet pages.",
  },
  upload: { label: "File upload", hue: 30, description: "Upload a document." },
};

export const dataSources: DataSource[] = [
  {
    id: "ds-web",
    kind: "website",
    name: "acme.io (public docs)",
    connected: true,
    lastSync: "3 hrs ago",
    docCount: 214,
    schedule: "daily",
    sensitivity: "public",
    scope: "https://acme.io/docs/*",
  },
  {
    id: "ds-upload",
    kind: "upload",
    name: "Company handbook (upload)",
    connected: true,
    lastSync: "yesterday",
    docCount: 1,
    schedule: "manual",
    sensitivity: "internal",
    scope: "handbook.md",
  },
];

export type EmbeddingModel =
  | "google/gemini-embedding-2"
  | "openai/text-embedding-3-large"
  | "openai/text-embedding-3-small"
  | "local/bge-large";

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  sourceIds: string[];
  docs: number;
  chunks: number;
  embeddingModel: EmbeddingModel;
  sensitivity: SensitivityLevel;
  updated: string;
  status: "current" | "updating" | "error";
  linkedDepartments: string[];
  linkedAgents: string[];
  roles: string[]; // roles allowed to use
  localOnly?: boolean;
  /** Set once the base is deleted (`DELETE /knowledge/bases/{id}`) -- this is
   * IRREVERSIBLE (it reduces the base's `KbChunk`s in the same transaction,
   * §12.5.1), so unlike Skill/Agent/Department there is no restore for this
   * field; it only drives a read-only "Deleted" badge. See the Design System
   * Consistency plan's DataSource/KnowledgeBase archive exception and Task
   * 20's report. */
  deletedAt?: string | null;
  nameTranslations?: Record<string, string>;
  descriptionTranslations?: Record<string, string>;
  /** ``internal`` (default) or a capa vector-index type id. */
  indexType?: string;
  /** Non-secret mapping for an external index. Never secrets. */
  indexConfig?: Record<string, unknown>;
  credentialId?: string | null;
}

export interface KnowledgeDocument {
  sourceUri: string;
  dataSourceId: string | null;
  kbId: string;
  chunks: number;
  createdAt: string | null;
  deletedAt: string | null;
  deletedReason: string | null;
  reducedAt: string | null;
}

export interface KbChunk {
  id: string;
  kbId: string;
  sourceUri: string;
  content: string;
  classification: string;
  chunkMetadata: Record<string, unknown>;
  createdAt: string;
}

export interface SimilarChunk extends KbChunk {
  similarity: number;
}

export const knowledgeBases: KnowledgeBase[] = [
  {
    id: "kb-sales",
    name: "Sales KB",
    description: "Pricing, playbooks, objection handling, competitor briefs.",
    sourceIds: ["ds-upload", "ds-web"],
    docs: 1498,
    chunks: 24_312,
    embeddingModel: "google/gemini-embedding-2",
    sensitivity: "internal",
    updated: "12 min ago",
    status: "current",
    linkedDepartments: ["vertrieb"],
    linkedAgents: ["vera", "leo", "nina"],
    roles: ["Sales", "Sales Lead", "Admin"],
  },
  {
    id: "kb-legal",
    name: "Legal KB",
    description: "Executed contracts, NDAs, DPA templates, jurisdiction notes.",
    sourceIds: ["ds-upload"],
    docs: 92,
    chunks: 3_804,
    embeddingModel: "local/bge-large",
    sensitivity: "restricted",
    updated: "yesterday",
    status: "current",
    linkedDepartments: [],
    linkedAgents: ["fin"],
    roles: ["Legal", "CFO", "Admin"],
    localOnly: true,
  },
  {
    id: "kb-product",
    name: "Product knowledge",
    description: "Feature specs, roadmap, release notes, product analytics.",
    sourceIds: ["ds-web"],
    docs: 738,
    chunks: 12_960,
    embeddingModel: "google/gemini-embedding-2",
    sensitivity: "confidential",
    updated: "1 hr ago",
    status: "updating",
    linkedDepartments: ["entwicklung", "marketing", "support"],
    linkedAgents: ["dex", "mara", "sam", "echo"],
    roles: ["Engineering", "Marketing", "Support", "Admin"],
  },
  {
    id: "kb-onboard",
    name: "Onboarding & Policies",
    description: "Handbook, GDPR guide, IT policies, expense rules.",
    sourceIds: ["ds-upload", "ds-web"],
    docs: 214,
    chunks: 4_180,
    embeddingModel: "openai/text-embedding-3-small",
    sensitivity: "internal",
    updated: "3 days ago",
    status: "current",
    linkedDepartments: ["vertrieb", "entwicklung", "marketing", "buchhaltung", "hr", "support"],
    linkedAgents: [],
    roles: ["All employees"],
  },
];

export function knowledgeBaseById(id: string) {
  return knowledgeBases.find((k) => k.id === id);
}

export function dataSourceById(id: string) {
  return dataSources.find((d) => d.id === id);
}
