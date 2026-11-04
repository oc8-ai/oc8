"""Seed data — reproduces the frontend mock (src/lib/*.ts) 1:1 for tenant ACME,
plus a minimal second tenant (Globex) used by isolation tests.

German overlays for the ACME showcase live in `demo_i18n.py` and are stored
under each row's `i18n.de` map so the UI can flip language without a second
tenant. Enable with `OC8_DEMO=true`:

- `docker-compose.demo.yml` — hosted walkthrough: ACME only + password admin
  (`OC8_DEMO_EMAIL` / `OC8_DEMO_PASSWORD`), `OC8_ENV=prod` (no open login).
- `docker-compose.dev.yml` — local hacking: ACME + unauthenticated `dev-login`
  (`OC8_ENV=dev`). Globex is skipped in both demo paths so password auth keeps
  its singleton-Organization invariant.

Runs as the schema-owner role, which bypasses RLS, so it can populate multiple
tenants in one pass when Globex is included.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from oc8 import models as m
from oc8.audit import append_event
from oc8.authz.permissions import role_kind
from oc8.config import get_settings
from oc8.constants import ACME_TENANT_ID, GLOBEX_TENANT_ID
from oc8.modelrouter.registry import canonical_provider
from oc8.seed.demo_i18n import (
    ACTIVITY_DE,
    AGENTS_DE,
    DEPARTMENTS_DE,
    ESCALATIONS_DE,
    KBS_DE,
    MODELS_DE,
    SKILLS_DE,
    SOURCES_DE,
    TASKS_DE,
    de_block,
)
from oc8.seed.department_templates import seed_department_templates

_NS = uuid.UUID("0192a000-0000-7000-8000-0000000000ff")


def det(*parts: Any) -> uuid.UUID:
    """Stable id derived from a slug path, so re-seeding is idempotent."""
    return uuid.uuid5(_NS, ":".join(str(p) for p in parts))


def _tool(enabled: int, read: int, write: int, send: int, approval: int | None) -> dict[str, Any]:
    return {
        "enabled": bool(enabled),
        "read": bool(read),
        "modify": bool(write or send),
        "approval_eur": approval,
    }


# Department frames — ceilings keyed by real MCP connection names (same
# vocabulary as capas/*/tool_pack.toml and a live install: hubspot, odoo,
# microsoft365, github, jira). Generic Lovable labels (crm/email/office) made
# the Tools/Guardrails table look unlike a real system. `coding` is the
# built-in coding-runtime frame key.
DEPT_FRAMES: dict[str, dict[str, tuple[int, int, int, int, int | None]]] = {
    "vertrieb": {
        "hubspot": (1, 1, 1, 1, 5000),
        "odoo": (1, 1, 1, 1, 5000),
        "microsoft365": (1, 1, 1, 1, 5000),
        "coding": (1, 1, 1, 0, None),
    },
    "entwicklung": {
        "github": (1, 1, 1, 1, None),
        "jira": (1, 1, 1, 1, None),
        "microsoft365": (1, 1, 0, 0, None),
        "coding": (1, 1, 1, 0, None),
    },
    "marketing": {
        "hubspot": (1, 1, 1, 0, None),
        "microsoft365": (1, 1, 1, 1, 2500),
        "jira": (1, 1, 1, 1, None),
        "coding": (1, 1, 1, 0, None),
    },
    "buchhaltung": {
        "odoo": (1, 1, 1, 1, 2500),
        "microsoft365": (1, 1, 1, 0, None),
        "coding": (1, 1, 1, 0, None),
    },
    "hr": {
        "microsoft365": (1, 1, 1, 0, None),
        "odoo": (1, 1, 0, 0, None),
        "coding": (1, 1, 1, 0, None),
    },
    "support": {
        "jira": (1, 1, 1, 1, None),
        "microsoft365": (1, 1, 1, 1, 200),
        "hubspot": (1, 1, 0, 0, None),
        "coding": (1, 1, 1, 0, None),
    },
}

# Skill `requires.tools` are frame keys (must match a DEPT_FRAMES entry, or
# assignment 422s via authz.pdp.missing_skill_requirements) mapped by the
# skill's own category. The seeded skills' human-readable tool names (e.g.
# "Accounting", "Procurement") live in `presentation.tools` for the UI instead.
SKILL_CATEGORY_TOOL_FRAMES: dict[str, list[str]] = {
    "finance": ["odoo", "microsoft365"],
    "sales": ["hubspot", "microsoft365"],
    "marketing": ["hubspot", "microsoft365"],
    "hr": ["microsoft365", "odoo"],
}

# Agent narrowings (permissions.ts agentPolicies) — subset/tighten only.
AGENT_NARROW: dict[str, dict[str, tuple[int, int, int, int, int | None]]] = {
    "leo": {"hubspot": (1, 1, 0, 0, None)},
    "nina": {"hubspot": (1, 1, 1, 1, 1000), "microsoft365": (1, 1, 1, 1, 1000)},
}

# departments[] from mock-data.ts
DEPARTMENTS: list[tuple[Any, ...]] = [
    (
        "vertrieb",
        "Sales",
        "sales",
        "Fill the pipeline",
        "+30% qualified leads by end of Q3",
        "Leads today",
        "18",
        82,
        "oklch(0.75 0.15 30)",
    ),
    (
        "entwicklung",
        "Engineering",
        "engineering",
        "Ship the feature backlog",
        "12 story points / sprint · release 2.4",
        "PRs in review",
        "3",
        74,
        "oklch(0.72 0.14 250)",
    ),
    (
        "marketing",
        "Marketing",
        "marketing",
        "Campaigns & content",
        "2 campaigns live, CAC < €180",
        "Campaigns live",
        "2",
        61,
        "oklch(0.75 0.15 340)",
    ),
    (
        "buchhaltung",
        "Accounting",
        "finance",
        "Invoices & reports",
        "Monthly close by BD+3",
        "Receipts processed",
        "37",
        68,
        "oklch(0.75 0.15 85)",
    ),
    (
        "hr",
        "People",
        "hr",
        "Review applications",
        "Time-to-interview < 5 days",
        "CVs in screening",
        "12",
        22,
        "oklch(0.72 0.14 320)",
    ),
    (
        "support",
        "Support",
        "support",
        "Resolve tickets",
        "First response < 15 min, CSAT > 4.5",
        "Tickets closed",
        "24",
        88,
        "oklch(0.74 0.15 175)",
    ),
]

# model_config rows (mock models[] plus Claude Haiku used by an agent).
MODELS: list[tuple[Any, ...]] = [
    (
        "Claude",
        "Claude 3.5 Sonnet",
        "cloud",
        "healthy",
        "1.2s",
        "$$",
        "Preferred for text & reasoning.",
    ),
    ("GPT", "GPT-4o", "cloud", "healthy", "0.9s", "$$$", "Multimodal, tool-use."),
    (
        "GPT",
        "GPT-4o mini",
        "cloud",
        "degraded",
        "2.4s",
        "$",
        "Higher latency reported since 09:00.",
    ),
    ("Mistral", "Mistral Large", "cloud", "healthy", "1.0s", "$", "EU region, low cost."),
    (
        "Ollama",
        "Llama 3.1 8B (local)",
        "local",
        "healthy",
        "1.8s",
        "$",
        "On-premise, no data leaves the VPC.",
    ),
    ("Claude", "Claude 3.5 Haiku", "cloud", "healthy", "1.1s", "$", "Fast, low-cost Claude."),
]
LLM_TO_MODEL = {
    "Claude 3.5 Sonnet": "Claude 3.5 Sonnet",
    "GPT-4o": "GPT-4o",
    "GPT-4o mini": "GPT-4o mini",
    "Mistral Large": "Mistral Large",
    "Llama 3.1 (local)": "Llama 3.1 8B (local)",
    "Claude 3.5 Haiku": "Claude 3.5 Haiku",
}

# agents[] from mock-data.ts — (slug, name, role, llm, provider, status, tools, lastAction,
# lastRun, tasksToday, guardrails, schedule, avatarColor, departmentId, isLead)
AGENTS: list[tuple[Any, ...]] = [
    (
        "vera",
        "Vera",
        "Sales Assistant",
        "Claude 3.5 Sonnet",
        "Claude",
        "warning",
        ["HubSpot", "Odoo", "Microsoft 365"],
        "Prepared quote for client Bauer GmbH — awaiting approval",
        "2 min ago",
        24,
        ["Max quote value €10,000", "No discounts >15%", "Approval required from €5,000"],
        "Mon–Fri, 08:00–18:00",
        "oklch(0.75 0.15 30)",
        "vertrieb",
        True,
    ),
    (
        "data",
        "Data",
        "Data Analysis",
        "GPT-4o",
        "GPT",
        "running",
        ["HubSpot", "Microsoft 365", "Jira"],
        "Generated weekly revenue report and shared to #sales",
        "34 sec ago",
        41,
        ["Read-only database access", "No PII in reports"],
        "Continuous",
        "oklch(0.70 0.15 220)",
        "marketing",
        False,
    ),
    (
        "doku",
        "Doku",
        "Internal Documentation",
        "Mistral Large",
        "Mistral",
        "running",
        ["GitHub", "Jira", "Microsoft 365"],
        "Summarized changes to API documentation",
        "7 min ago",
        18,
        ["Internal wikis only", "No code changes"],
        "On-Demand",
        "oklch(0.70 0.14 155)",
        "entwicklung",
        False,
    ),
    (
        "hera",
        "Hera",
        "HR Assistant",
        "Llama 3.1 (local)",
        "Ollama",
        "paused",
        ["Microsoft 365", "Odoo"],
        "Paused by admin — GDPR review",
        "2 hrs ago",
        6,
        ["Local LLM only", "No external APIs", "Employee data encrypted"],
        "Mon–Fri, 09:00–17:00",
        "oklch(0.72 0.14 320)",
        "hr",
        True,
    ),
    (
        "fin",
        "Fin",
        "Accounting",
        "Claude 3.5 Sonnet",
        "Claude",
        "running",
        ["Odoo", "Microsoft 365"],
        "Categorized 12 incoming invoices and transferred to the accounting system",
        "12 min ago",
        37,
        ["Approval for bookings >€2,500", "Four-eyes principle active"],
        "Weekdays, 07:00–20:00",
        "oklch(0.75 0.15 85)",
        "buchhaltung",
        True,
    ),
    (
        "ops",
        "Ops",
        "IT Monitoring",
        "GPT-4o mini",
        "GPT",
        "error",
        ["Jira", "Microsoft 365", "GitHub"],
        "Error: Connection to the monitoring API dropped (401)",
        "4 min ago",
        16,
        ["No production deploys", "Create alerts only"],
        "24/7",
        "oklch(0.68 0.20 25)",
        "support",
        False,
    ),
    (
        "leo",
        "Leo",
        "Outbound Sales",
        "GPT-4o mini",
        "GPT",
        "running",
        ["HubSpot", "Microsoft 365"],
        "Sent 24 cold emails to 'Manufacturing DACH' segment",
        "1 min ago",
        28,
        ["Max 50 emails/day", "B2B addresses only"],
        "Mon–Fri, 08:00–17:00",
        "oklch(0.72 0.14 45)",
        "vertrieb",
        False,
    ),
    (
        "nina",
        "Nina",
        "Lead Qualification",
        "Claude 3.5 Haiku",
        "Claude",
        "running",
        ["HubSpot", "Microsoft 365"],
        "Scored 9 new leads — 3 marked as 'Hot'",
        "3 min ago",
        21,
        ["Scoring only, no customer contact"],
        "Mon–Fri, 09:00–18:00",
        "oklch(0.74 0.14 15)",
        "vertrieb",
        False,
    ),
    (
        "dex",
        "Dex",
        "Engineering Lead",
        "Claude 3.5 Sonnet",
        "Claude",
        "running",
        ["GitHub", "Jira"],
        "Completed PR review for #482 — 2 comments",
        "40 sec ago",
        34,
        ["No force-pushes to main", "Approval for prod deploys"],
        "Mon–Fri, 09:00–19:00",
        "oklch(0.72 0.14 250)",
        "entwicklung",
        True,
    ),
    (
        "ada",
        "Ada",
        "Backend Development",
        "GPT-4o",
        "GPT",
        "running",
        ["GitHub", "Jira"],
        "Implemented 'Batch Export' feature — tests green",
        "6 min ago",
        19,
        ["Feature branches only", "Coverage >80%"],
        "Continuous",
        "oklch(0.74 0.15 200)",
        "entwicklung",
        False,
    ),
    (
        "kern",
        "Kern",
        "QA & Tests",
        "Mistral Large",
        "Mistral",
        "warning",
        ["GitHub", "Jira"],
        "E2E test run: 2 regressions found — awaiting triage",
        "8 min ago",
        12,
        ["No prod database access"],
        "Continuous",
        "oklch(0.72 0.14 130)",
        "entwicklung",
        False,
    ),
    (
        "mara",
        "Mara",
        "Marketing Lead",
        "Claude 3.5 Sonnet",
        "Claude",
        "running",
        ["HubSpot", "Microsoft 365"],
        "Rolled out 'Q3 Launch' campaign on the social network",
        "5 min ago",
        15,
        ["Budget cap €2,000/week", "Brand guidelines"],
        "Mon–Fri, 08:00–18:00",
        "oklch(0.75 0.15 340)",
        "marketing",
        True,
    ),
    (
        "tim",
        "Tim",
        "Content Editor",
        "GPT-4o",
        "GPT",
        "running",
        ["Microsoft 365", "HubSpot"],
        "Blog post 'Agents in Practice' in review",
        "11 min ago",
        8,
        ["No auto-publishing"],
        "Mon–Fri, 09:00–17:00",
        "oklch(0.74 0.15 300)",
        "marketing",
        False,
    ),
    (
        "cent",
        "Cent",
        "Travel Expenses & Receipts",
        "Mistral Large",
        "Mistral",
        "running",
        ["Odoo", "Microsoft 365"],
        "OCR-processed and reviewed 18 travel receipts",
        "15 min ago",
        22,
        ["Max single receipt €500 without approval"],
        "Weekdays, 08:00–17:00",
        "oklch(0.75 0.15 70)",
        "buchhaltung",
        False,
    ),
    (
        "sam",
        "Sam",
        "Support Lead",
        "Claude 3.5 Sonnet",
        "Claude",
        "running",
        ["Jira", "Microsoft 365", "HubSpot"],
        "Resolved ticket #4412 — SSO connection issue",
        "2 min ago",
        31,
        ["No refunds without approval", "PII filter"],
        "Mon–Sat, 08:00–20:00",
        "oklch(0.74 0.15 175)",
        "support",
        True,
    ),
    (
        "echo",
        "Echo",
        "First-Level Support",
        "GPT-4o mini",
        "GPT",
        "running",
        ["Jira", "Microsoft 365"],
        "Generated 14 answers from knowledge base",
        "45 sec ago",
        47,
        ["Standard replies only", "Escalate from level 2"],
        "24/7",
        "oklch(0.74 0.15 155)",
        "support",
        False,
    ),
]

STATUS_MAP = {
    "running": "running",
    "warning": "waiting_for_approval",
    "error": "error",
    "paused": "paused",
}

# skills.ts
SKILLS: list[tuple[Any, ...]] = [
    (
        "sk-invoice-check",
        "Invoice Review & Booking",
        "Validates incoming invoices against POs, extracts line items and books them to the "
        "correct GL account.",
        "finance",
        "local",
        "1.4.2",
        "oc8 core",
        ["Odoo", "Microsoft 365"],
        ["Chart of Accounts", "Vendor Master"],
        ["Amounts above €10 000 require human approval", "Reject invoices without valid VAT ID"],
        "Given an invoice PDF, extract header + line items, match against open POs, book to the "
        "correct GL account and route to approval if thresholds apply.",
        4,
        None,
        None,
        "2026-06-28",
    ),
    (
        "sk-lead-qualify",
        "Lead Qualification (BANT)",
        "Enriches inbound leads and scores them by BANT criteria before routing to AE.",
        "sales",
        "local",
        "2.1.0",
        "Sales Ops",
        ["HubSpot", "Microsoft 365"],
        ["ICP Definition", "Sales Playbook"],
        ["Never contact leads on the do-not-call list"],
        "Enrich the lead, evaluate Budget / Authority / Need / Timeline, produce a score 0-100 "
        "and route.",
        2,
        None,
        None,
        "2026-06-14",
    ),
    (
        "sk-contract-summary",
        "Contract Summarisation",
        "Summarises contracts with obligations, terms, renewal dates and risk flags.",
        "legal",
        "local",
        "1.0.7",
        "Legal Team",
        ["Microsoft 365"],
        ["Legal Templates"],
        ["Do not give legal advice", "Flag jurisdiction changes"],
        "Parse the contract, produce structured summary with parties, term, renewal, obligations "
        "and risks.",
        3,
        None,
        None,
        "2026-05-30",
    ),
    (
        "sk-ticket-triage",
        "Support Ticket Triage",
        "Classifies inbound tickets, sets priority and drafts a first response.",
        "support",
        "store",
        "3.2.1",
        "Community · helpdesk-labs",
        ["Jira", "Microsoft 365"],
        ["Help Center"],
        ["Escalate legal or safety issues immediately"],
        "Classify by product area, set priority, propose response draft citing help-center "
        "articles.",
        1,
        4210,
        "free",
        "2026-07-01",
    ),
    (
        "sk-onboarding",
        "New-Hire Onboarding",
        "Coordinates equipment, accounts and first-week schedule for new hires.",
        "hr",
        "store",
        "1.1.0",
        "Community · people-ops",
        ["Odoo", "Microsoft 365"],
        [],
        ["Never share private employee data outside HR channel"],
        "Given a new-hire record, provision accounts, order equipment, schedule intro meetings.",
        0,
        892,
        12,
        "2026-06-20",
    ),
    (
        "sk-inventory-recon",
        "Inventory Reconciliation",
        "Reconciles physical stock against ERP and posts adjustment journals.",
        "operations",
        "store",
        "0.9.4",
        "Community · ops-guild",
        ["Odoo", "Microsoft 365"],
        [],
        ["Adjustments above 2% variance require approval"],
        "Compare cycle-count file with ERP quantities, produce variance report, post approved "
        "adjustments.",
        0,
        317,
        "free",
        "2026-05-11",
    ),
]

# integrations[] — product names matching capa connection keys
INTEGRATIONS: list[tuple[Any, ...]] = [
    ("odoo", "Odoo", "ERP", True, ["fin", "vera"], "CRM, sales, invoices, accounting.", 280),
    ("hubspot", "HubSpot", "CRM", True, ["vera", "nina", "leo"], "Contacts, deals, pipeline.", 25),
    (
        "microsoft365",
        "Microsoft 365",
        "Productivity",
        True,
        ["vera", "hera", "fin"],
        "Mail, calendar, Teams, SharePoint, OneDrive.",
        220,
    ),
    ("jira", "Jira", "Issues", True, ["data", "ops", "dex"], "Issues, boards, alerts.", 320),
    (
        "github",
        "GitHub",
        "Engineering",
        True,
        ["doku", "ops", "dex", "ada"],
        "Repos, pull requests, issues.",
        250,
    ),
    (
        "google_workspace",
        "Google Workspace",
        "Productivity",
        False,
        [],
        "Gmail, Drive, Calendar, Docs.",
        155,
    ),
    ("gitea", "Gitea", "Engineering", False, [], "Self-hosted git, PRs, issues.", 200),
]

# dataSources[]
SOURCES: list[tuple[Any, ...]] = [
    # website (core connector) — a real, crawlable public docs site.
    (
        "ds-web",
        "website",
        "acme.io (public docs)",
        True,
        "3 hrs ago",
        214,
        "daily",
        "public",
        "https://acme.io/docs/*",
        {"url": "https://acme.io/docs/"},
    ),
    # upload (core connector) — a real uploaded document, so "connected" is honest.
    (
        "ds-upload",
        "upload",
        "Company handbook (upload)",
        True,
        "yesterday",
        1,
        "manual",
        "internal",
        "handbook.md",
        {
            "filename": "handbook.md",
            "content": "# Company handbook\n\nWelcome to the demo tenant.",
            "content_type": "text/markdown",
        },
    ),
    # Vendor sources (Google Drive, S3, etc.) are NOT seeded: they arrive when the
    # operator installs and enables the corresponding connector plugin and then
    # connects an account. The core seeds no vendor connector it cannot run.
]

# knowledgeBases[]
KBS: list[tuple[Any, ...]] = [
    (
        "kb-sales",
        "Sales KB",
        "Pricing, playbooks, objection handling, competitor briefs.",
        ["ds-upload", "ds-web"],
        1498,
        24312,
        "google/gemini-embedding-2",
        "internal",
        "12 min ago",
        "current",
        ["vertrieb"],
        ["vera", "leo", "nina"],
        ["Sales", "Sales Lead", "Admin"],
        False,
    ),
    (
        "kb-legal",
        "Legal KB",
        "Executed contracts, NDAs, DPA templates, jurisdiction notes.",
        ["ds-upload"],
        92,
        3804,
        "local/bge-large",
        "restricted",
        "yesterday",
        "current",
        [],
        ["fin"],
        ["Legal", "CFO", "Admin"],
        True,
    ),
    (
        "kb-product",
        "Product knowledge",
        "Feature specs, roadmap, release notes, product analytics.",
        ["ds-web"],
        738,
        12960,
        "google/gemini-embedding-2",
        "confidential",
        "1 hr ago",
        "updating",
        ["entwicklung", "marketing", "support"],
        ["dex", "mara", "sam", "echo"],
        ["Engineering", "Marketing", "Support", "Admin"],
        False,
    ),
    (
        "kb-onboard",
        "Onboarding & Policies",
        "Handbook, GDPR guide, IT policies, expense rules.",
        ["ds-upload", "ds-web"],
        214,
        4180,
        "openai/text-embedding-3-small",
        "internal",
        "3 days ago",
        "current",
        ["vertrieb", "entwicklung", "marketing", "buchhaltung", "hr", "support"],
        [],
        ["All employees"],
        False,
    ),
]

# tasks[] — (slug, deptSlug, title, agentSlug, column, meta)
TASKS: list[tuple[Any, ...]] = [
    ("t-v1", "vertrieb", "Quote Bauer GmbH (€7,400)", "vera", "waiting", "Approval > €5,000"),
    ("t-v2", "vertrieb", "Follow-up Nordheim AG", "vera", "in_progress", None),
    ("t-v3", "vertrieb", "50 cold emails 'Manufacturing'", "leo", "in_progress", None),
    ("t-v4", "vertrieb", "Lead scoring W28", "nina", "in_progress", None),
    ("t-v5", "vertrieb", "Prepare discovery call", "vera", "backlog", None),
    ("t-v6", "vertrieb", "Import contacts from the CRM", "leo", "backlog", None),
    ("t-v7", "vertrieb", "Qualified 9 leads", "nina", "done", None),
    ("t-v8", "vertrieb", "Sent quote to Meier AG", "vera", "done", None),
    ("t-e1", "entwicklung", "PR #482: Batch Export", "ada", "waiting", "Review by Dex"),
    ("t-e2", "entwicklung", "Triage regressions", "kern", "waiting", "2 failures"),
    ("t-e3", "entwicklung", "Refactor SDK v3", "dex", "in_progress", None),
    ("t-e4", "entwicklung", "Update API v3 docs", "doku", "in_progress", None),
    ("t-e5", "entwicklung", "Extend E2E suite", "kern", "backlog", None),
    ("t-e6", "entwicklung", "Bug #911: Session timeout", "ada", "backlog", None),
    ("t-e7", "entwicklung", "PR #479 merged", "dex", "done", None),
    ("t-m1", "marketing", "Blog post 'Agents in practice'", "tim", "waiting", "Publish approval"),
    ("t-m2", "marketing", "Social campaign Q3", "mara", "in_progress", None),
    ("t-m3", "marketing", "Ads analysis", "data", "in_progress", None),
    ("t-m4", "marketing", "Draft newsletter W29", "tim", "backlog", None),
    ("t-m5", "marketing", "Campaign 'Spring' completed", "mara", "done", None),
    ("t-b1", "buchhaltung", "Vendor Meier & Co. R-0331", "fin", "waiting", "€3,280"),
    ("t-b2", "buchhaltung", "Incoming invoices W28", "fin", "in_progress", None),
    ("t-b3", "buchhaltung", "Travel expenses sales team", "cent", "in_progress", None),
    ("t-b4", "buchhaltung", "VAT return July", "fin", "backlog", None),
    ("t-b5", "buchhaltung", "Booked 12 receipts", "cent", "done", None),
    ("t-h1", "hr", "Screen 12 'Backend' CVs", "hera", "backlog", None),
    ("t-h2", "hr", "Wait for GDPR review", "hera", "waiting", "Admin pause"),
    ("t-s1", "support", "Ticket #4498 refund", "sam", "waiting", "Approval > €200"),
    ("t-s2", "support", "Investigate SSO outage", "ops", "in_progress", "Incident PROD-4412"),
    ("t-s3", "support", "14 first-level tickets", "echo", "in_progress", None),
    ("t-s4", "support", "Update knowledge base", "sam", "backlog", None),
    ("t-s5", "support", "Resolved ticket #4412", "sam", "done", None),
]
TASK_STATE = {
    "backlog": "backlog",
    "in_progress": "in_progress",
    "waiting": "waiting_for_approval",
    "done": "done",
}

# activity[] — (slug, agentSlug, status, message, time, detail)
ACTIVITY: list[tuple[Any, ...]] = [
    (
        "a1",
        "data",
        "success",
        "Sent revenue report W27",
        "14:42",
        "Recipients: #sales, cfo@acme.io. Attachment: revenue-w27.pdf",
    ),
    ("a2", "vera", "warning", "Approval requested: Bauer GmbH quote (€7,400)", "14:38", None),
    (
        "a3",
        "fin",
        "success",
        "Transferred 12 incoming invoices to the accounting system",
        "14:30",
        "Batch ID ACC-B-8391 · booking account 3400",
    ),
    ("a4", "ops", "error", "Monitoring API returns 401 — check API key", "14:22", None),
    ("a5", "doku", "success", "Generated changelog from 8 pull requests", "14:11", None),
    ("a6", "vera", "info", "Scanned new lead email from info@holtmann.de", "13:57", None),
    ("a7", "data", "success", "Updated 'Pipeline Health' dashboard", "13:42", None),
    ("a8", "hera", "info", "Agent paused by admin@oc8.io", "12:04", None),
    ("a9", "fin", "warning", "Approval requested: R-2026-0331 (€3,280)", "11:51", None),
    ("a10", "doku", "success", "Bundled onboarding wiki for Product X", "11:12", None),
]

# escalations[] -> approval_request (pending)
ESCALATIONS: list[tuple[Any, ...]] = [
    (
        "e1",
        "vera",
        "Vera wants to send a quote",
        "Bauer GmbH — 2026 maintenance contract, incl. remote-service add-on.",
        "€7,400",
        "2 min ago",
    ),
    (
        "e2",
        "fin",
        "Fin wants to approve booking",
        "Vendor Meier & Co. — invoice R-2026-0331, unusually high amount.",
        "€3,280",
        "9 min ago",
    ),
    (
        "e3",
        "vera",
        "Vera wants to grant a discount",
        "Client Nordheim AG requests a 12% discount on annual contract.",
        "-€4,320",
        "21 min ago",
    ),
]

BUILTIN_ROLES: list[str] = ["org_admin", "dept_manager", "operator", "auditor", "agent_default"]


def _frame_json(tid: uuid.UUID, dept_slug: str, kb_ids: list[str]) -> dict[str, Any]:
    tools = {k: _tool(*v) for k, v in DEPT_FRAMES.get(dept_slug, {}).items()}
    return {
        "tools": tools,
        "kbs": [str(det(tid, "kb", k)) for k in kb_ids],
        "memory": {"department": ["read", "write"], "company": ["read"]},
    }


def _narrow_json(agent_slug: str) -> dict[str, Any]:
    n = AGENT_NARROW.get(agent_slug)
    if not n:
        return {}
    return {"tools": {k: _tool(*v) for k, v in n.items()}}


async def _seed_acme(session: AsyncSession) -> None:
    tid = ACME_TENANT_ID
    # Disposable test tenants monkeypatch `ACME_TENANT_ID` but must not collide
    # on the global unique `organization.slug` with the real ACME row.
    from oc8.constants import ACME_TENANT_ID as canonical_acme

    is_canonical = tid == canonical_acme
    session.add(
        m.Organization(
            id=tid,
            slug="acme" if is_canonical else f"acme-{tid.hex[:8]}",
            name="ACME Industries" if is_canonical else f"ACME ({tid.hex[:8]})",
            tier="standard",
            region="eu",
        )
    )

    # `kind` derived per name, never uniform: this loop builds all five built-in
    # roles, and one of them is the AGENT's. A uniform 'human' here is every
    # seeded agent granted no tool rights the moment the PDP reads the column,
    # because `_seed_acme` points every agent's `role_id` at one of these rows.
    for r in BUILTIN_ROLES:
        session.add(
            m.Role(id=det(tid, "role", r), tenant_id=tid, name=r, builtin=True, kind=role_kind(r))
        )

    for provider, name, locality, status_, latency, cost_tier, note in MODELS:
        is_ollama = canonical_provider(provider) == "ollama"
        model_tag = get_settings().default_model if is_ollama else name
        cost_meta: dict[str, Any] = {"cost_tier": cost_tier, "note": note}
        model_de = MODELS_DE.get(name)
        if model_de:
            cost_meta["i18n"] = de_block(model_de)
        session.add(
            m.ModelConfig(
                id=det(tid, "model", name),
                tenant_id=tid,
                provider=provider,
                model=model_tag,
                locality=locality,
                display_name=name,
                cost_meta=cost_meta,
                health={"status": status_, "latency": latency},
            )
        )

    # KBs (docs/chunks/updated/roles kept in freshness to avoid extra columns).
    dept_kbs: dict[str, list[str]] = {}
    for kb in KBS:
        (
            slug,
            name,
            desc,
            src_ids,
            docs,
            chunks,
            emb,
            sens,
            updated,
            status_,
            linked_depts,
            linked_agents,
            roles,
            local_only,
        ) = kb
        session.add(
            m.KnowledgeBase(
                id=det(tid, "kb", slug),
                tenant_id=tid,
                name=name,
                description=desc,
                embedding_model=emb,
                classification=sens,
                source_ids=[str(det(tid, "source", s)) for s in src_ids],
                status=status_,
                local_only=local_only,
                freshness={
                    "docs": docs,
                    "chunks": chunks,
                    "updated": updated,
                    "roles": roles,
                    "i18n": de_block(KBS_DE.get(slug)),
                },
            )
        )
        for d in linked_depts:
            dept_kbs.setdefault(d, []).append(slug)
            session.add(
                m.KnowledgeGrant(
                    id=det(tid, "grant", slug, "dept", d),
                    tenant_id=tid,
                    kb_id=det(tid, "kb", slug),
                    grantee_type="department",
                    grantee_id=det(tid, "dept", d),
                )
            )
        for a in linked_agents:
            session.add(
                m.KnowledgeGrant(
                    id=det(tid, "grant", slug, "agent", a),
                    tenant_id=tid,
                    kb_id=det(tid, "kb", slug),
                    grantee_type="agent",
                    grantee_id=det(tid, "agent", a),
                )
            )

    for slug, name, icon, goal, okr, kpi_label, kpi_value, activity, accent in DEPARTMENTS:
        dept_de = DEPARTMENTS_DE.get(slug)
        session.add(
            m.Department(
                id=det(tid, "dept", slug),
                tenant_id=tid,
                name=name,
                goal=goal,
                frame=_frame_json(tid, slug, dept_kbs.get(slug, [])),
                presentation={
                    "icon": icon,
                    "okr": okr,
                    "kpi_label": kpi_label,
                    "kpi_value": kpi_value,
                    "activity": activity,
                    "accent": accent,
                    "slug": slug,
                    "i18n": de_block(dept_de),
                },
            )
        )

    for a in AGENTS:
        (
            slug,
            name,
            role,
            llm,
            provider,
            status_,
            tools,
            last_action,
            last_run,
            tasks_today,
            guardrails,
            schedule,
            avatar,
            dept_slug,
            is_lead,
        ) = a
        model_name = LLM_TO_MODEL.get(llm)
        session.add(
            m.Agent(
                id=det(tid, "agent", slug),
                tenant_id=tid,
                department_id=det(tid, "dept", dept_slug),
                name=name,
                role_id=det(tid, "role", "agent_default"),
                role_title=role,
                mission=f"{role} for the {dept_slug} department.",
                model_config_id=det(tid, "model", model_name) if model_name else None,
                narrowing=_narrow_json(slug),
                is_team_lead=is_lead,
                status=STATUS_MAP[status_],
                trust_level="first_party",
                definition={"oc8_agent": 1, "name": slug, "mission": role},
                presentation={
                    "llm": llm,
                    "provider": provider,
                    "tools": tools,
                    "guardrails": guardrails,
                    "schedule": schedule,
                    "last_action": last_action,
                    "last_run": last_run,
                    "tasks_today": tasks_today,
                    "avatar_color": avatar,
                    "slug": slug,
                    "i18n": de_block(AGENTS_DE.get(slug)),
                },
            )
        )
        session.add(
            m.MemoryStore(
                id=det(tid, "mem", "agent", slug),
                tenant_id=tid,
                tier="agent",
                owner_id=det(tid, "agent", slug),
            )
        )

    for slug, _n, _i, *_rest in DEPARTMENTS:
        session.add(
            m.MemoryStore(
                id=det(tid, "mem", "dept", slug),
                tenant_id=tid,
                tier="department",
                owner_id=det(tid, "dept", slug),
            )
        )
    session.add(
        m.MemoryStore(id=det(tid, "mem", "company"), tenant_id=tid, tier="company", owner_id=tid)
    )

    for s in SKILLS:
        (
            slug,
            name,
            desc,
            cat,
            origin,
            version,
            author,
            tools,
            knowledge,
            guardrails,
            instr,
            used,
            installs,
            price,
            updated,
        ) = s
        skill_id = det(tid, "skill", slug)
        ver_id = det(tid, "skillver", slug, version)
        skill_de = SKILLS_DE.get(slug, {})
        requires_tools = SKILL_CATEGORY_TOOL_FRAMES.get(cat, ["email"])
        session.add(
            m.Skill(
                id=skill_id,
                tenant_id=tid,
                name=name,
                category=cat,
                description=desc,
                author=author,
                origin=origin,
                trust_level="first_party" if origin == "local" else "community",
                current_version_id=ver_id,
            )
        )
        session.add(
            m.SkillVersion(
                id=ver_id,
                tenant_id=tid,
                skill_id=skill_id,
                semver=version,
                artifact_hash=det(tid, "hash", slug, version).bytes,
                definition={
                    "oc8_skill": 1,
                    "id": slug,
                    "version": version,
                    "instruction": instr,
                    "requires": {"tools": requires_tools, "kbs": knowledge},
                    "guardrails": guardrails,
                    "presentation": {
                        "tools": tools,
                        "knowledge": knowledge,
                        "guardrails": guardrails,
                        "used_by_agents": used,
                        "installs": installs,
                        "price": price,
                        "updated_at": updated,
                        "i18n": de_block(skill_de),
                    },
                },
            )
        )

    for key, name, cat, connected, used_by, desc, hue in INTEGRATIONS:
        session.add(
            m.Integration(
                id=det(tid, "integration", key),
                tenant_id=tid,
                key=key,
                name=name,
                category=cat,
                connected=connected,
                description=desc,
                hue=hue,
                used_by=used_by,
            )
        )

    # No seeded McpConnection rows: empty "Untested / Test connection" cards
    # under Capas look like a broken install next to the real enabled capas.
    # Agent Tools still list hubspot/odoo/… via department frame keys alone.

    for slug, kind, name, connected, last_sync, docs, sched, sens, scope, extra in SOURCES:
        session.add(
            m.DataSource(
                id=det(tid, "source", slug),
                tenant_id=tid,
                connector_type=kind,
                name=name,
                connected=connected,
                last_sync_at=last_sync,
                doc_count=docs or 0,
                classification=sens or "internal",
                config={
                    "schedule": sched,
                    "scope": scope,
                    "i18n": de_block(SOURCES_DE.get(slug)),
                    **extra,
                },
            )
        )

    for slug, dept_slug, title, agent_slug, column, meta in TASKS:
        session.add(
            m.Task(
                id=det(tid, "task", slug),
                tenant_id=tid,
                department_id=det(tid, "dept", dept_slug),
                assigned_agent_id=det(tid, "agent", agent_slug),
                title=title,
                state=TASK_STATE[column],
                meta_label=meta,
                payload={"slug": slug, "i18n": de_block(TASKS_DE.get(slug))},
            )
        )

    for slug, agent_slug, status_, message, time_, detail in ACTIVITY:
        session.add(
            m.ActivityEvent(
                id=det(tid, "activity", slug),
                tenant_id=tid,
                agent_id=det(tid, "agent", agent_slug),
                status=status_,
                message=message,
                detail=detail,
                i18n=de_block(ACTIVITY_DE.get(slug)),
                ts=dt.datetime(
                    2026, 7, 9, int(time_.split(":")[0]), int(time_.split(":")[1]), tzinfo=dt.UTC
                ),
            )
        )

    # The department each agent stands in, so a seeded approval is filed the same
    # way `raise_approval` files a real one. Without it every demo approval is
    # born `department_id IS NULL` -- which means TENANT-WIDE, visible only to an
    # org_admin -- and a seeded tenant would show a seat-holding Head of Sales an
    # empty queue on the one screen this slice exists to build. The seed cannot
    # call `raise_approval`: these rows carry deterministic ids and must not be
    # announced on a messenger.
    department_of_agent = {row[0]: det(tid, "dept", row[13]) for row in AGENTS}
    for slug, agent_slug, title, detail, amount, time_ in ESCALATIONS:
        esc_de = ESCALATIONS_DE.get(slug)
        session.add(
            m.ApprovalRequest(
                id=det(tid, "approval", slug),
                tenant_id=tid,
                agent_id=det(tid, "agent", agent_slug),
                department_id=department_of_agent[agent_slug],
                action_type="tool_send",
                status="pending",
                title=title,
                detail=detail,
                amount_text=amount,
                payload={"time": time_, "i18n": de_block(esc_de)},
            )
        )

    await seed_department_templates(session, tenant_id=tid)

    if get_settings().is_demo:
        from oc8.seed.demo_showcase import seed_demo_showcase

        await seed_demo_showcase(session, tenant_id=tid)

    await session.flush()
    await append_event(
        session,
        tenant_id=tid,
        actor_type="system",
        actor_id=None,
        category="admin",
        action="seed.loaded",
        resource={"tenant": "acme"},
    )


async def _seed_demo_admin(session: AsyncSession) -> None:
    """Password-login admin for the ACME showcase (hosted demo, OC8_ENV=prod).

    Community password auth requires a singleton Organization and at least one
    `org_member` with a password_hash. The ACME seed already supplies the
    singleton; this writes the member. Skipped when `OC8_DEMO_PASSWORD` is
    empty so a forgotten env never invents a guessable secret.
    """
    from oc8.auth.password import PasswordHashingError, hash_password

    settings = get_settings()
    # Same canonical spelling as PasswordSetupRequest (`_normalized_address`).
    email = (settings.demo_email or "").strip().lower()
    password = settings.demo_password or ""
    if not email or not password:
        print(
            "demo seed: OC8_DEMO_EMAIL/OC8_DEMO_PASSWORD not both set — "
            "skipping demo admin (password login will have nobody to authenticate)"
        )
        return
    if len(password) < 8:
        raise ValueError("OC8_DEMO_PASSWORD must be at least 8 characters")

    tid = ACME_TENANT_ID
    try:
        password_hash = hash_password(password)
    except PasswordHashingError as err:
        raise ValueError(f"OC8_DEMO_PASSWORD could not be hashed: {err}") from err

    admin_role_id = det(tid, "role", "org_admin")
    session.add(
        m.OrgMember(
            id=det(tid, "member", "demo-admin"),
            tenant_id=tid,
            subject=email,
            email=email,
            # Match password_setup's subject_uuid derivation (local auth, not
            # subject_uuid_for / messenger door).
            subject_uuid=uuid.uuid5(uuid.NAMESPACE_URL, f"oc8:local:{email}"),
            display_name="Demo Admin",
            password_hash=password_hash,
            all_departments=True,
            role_id=admin_role_id,
            # No totp_grace_started_at: demo walkthroughs must not force 2FA
            # enrollment before anyone can open the Office.
        )
    )
    print(f"demo seed: password login ready for {email}")


async def _seed_globex(session: AsyncSession) -> None:
    tid = GLOBEX_TENANT_ID
    session.add(
        m.Organization(
            id=tid, slug="globex", name="Globex Corporation", tier="standard", region="eu"
        )
    )
    # Globex has one role row and it is a person's. `kind` is said out loud
    # rather than left to the column default, so that every writer of a `role`
    # row in this tree names the population it is writing.
    session.add(
        m.Role(
            id=det(tid, "role", "org_admin"),
            tenant_id=tid,
            name="org_admin",
            builtin=True,
            kind=role_kind("org_admin"),
        )
    )
    session.add(
        m.Department(
            id=det(tid, "dept", "ops"),
            tenant_id=tid,
            name="Operations",
            goal="Keep the lights on",
            frame={"tools": {}, "kbs": [], "memory": {}},
            presentation={"icon": "support", "slug": "ops"},
        )
    )
    session.add(
        m.Agent(
            id=det(tid, "agent", "gx1"),
            tenant_id=tid,
            department_id=det(tid, "dept", "ops"),
            name="Gizmo",
            role_title="Ops Bot",
            mission="Globex ops",
            status="running",
            definition={"oc8_agent": 1, "name": "gx1"},
            presentation={
                "llm": "Llama 3.1 8B (local)",
                "provider": "Ollama",
                "tools": [],
                "guardrails": [],
                "schedule": "24/7",
                "last_action": "",
                "last_run": "",
                "tasks_today": 0,
                "avatar_color": "oklch(0.7 0.1 200)",
                "slug": "gx1",
            },
        )
    )
    await seed_department_templates(session, tenant_id=tid)

    await session.flush()
    await append_event(
        session,
        tenant_id=tid,
        actor_type="system",
        actor_id=None,
        category="admin",
        action="seed.loaded",
        resource={"tenant": "globex"},
    )


class TenantAuthorizationWouldBeLost(RuntimeError):
    """`--reset` would TRUNCATE authorization somebody typed.

    A distinct type rather than a bare RuntimeError because it is the one refusal
    in this module that is about the customer's data rather than about the seed's
    own state, and a caller that wants to override it should have to name it.
    """


async def tenant_defined_role_count(session: AsyncSession) -> int:
    """How many roles a tenant composed itself, across the whole database.

    Across the whole database on purpose: `--reset` is not scoped to a tenant, it
    TRUNCATEs everything, so the question that matches what the command does is
    the global one.

    Built-in rows are not counted -- they are written by `create_tenant` and by
    this file, and re-seeding reproduces them exactly -- and neither are
    soft-deleted ones, which are tombstones holding a name that nothing resolves
    through. Counting either would make `--reset` refuse on every database that
    has ever had a tenant, which is the same as not having the flag, and a guard
    everybody works around is not a guard.
    """
    return int(
        (
            await session.execute(
                text("SELECT count(*) FROM role WHERE NOT builtin AND deleted_at IS NULL")
            )
        ).scalar_one()
    )


async def _refuse_reset_over_tenant_roles(session: AsyncSession) -> None:
    """Refuse `--reset` once a tenant has composed a role of its own.

    `--reset` TRUNCATEs every table in the metadata, which now includes
    `role_permission` and `org_member.role_id`, and `docker-compose.yml` runs
    `oc8 seed` on start when `OC8_SEED_ON_START=true`. This is the FIRST TIME
    that flag can destroy something a customer typed rather than something this
    file wrote -- a demo tenant is reproducible, a tenant's authorization model
    is not, and nothing about the failure would be visible afterwards except that
    forty people are administrators again.
    """
    held = await tenant_defined_role_count(session)
    if held:
        raise TenantAuthorizationWouldBeLost(
            f"refusing --reset: {held} tenant-defined role(s) exist, and a reset "
            "would TRUNCATE them along with every assignment pointing at them. "
            "Roles and their grants are configuration a customer typed; there is "
            "no backup of them here. Drop the database explicitly if that is "
            "really what you mean."
        )


async def run_seed(reset: bool = False) -> None:
    settings = get_settings()
    engine = create_async_engine(settings.migration_async_url)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sm() as session:
            if reset:
                await _refuse_reset_over_tenant_roles(session)
                tables = ", ".join(t.name for t in reversed(m.Organization.metadata.sorted_tables))
                await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
                await session.commit()
            async with sm() as session2:
                await _seed_acme(session2)
                # Hosted demo needs a singleton Organization for password auth
                # (`_get_singleton_organization`). Globex is only for isolation
                # tests under the unauthenticated seed path.
                if settings.is_demo:
                    await _seed_demo_admin(session2)
                else:
                    await _seed_globex(session2)
                await session2.commit()
    finally:
        await engine.dispose()
    if settings.is_demo:
        print("seed complete: ACME demo (password login)")
    else:
        print("seed complete: ACME + Globex")
