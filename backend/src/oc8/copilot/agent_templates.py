"""Agent ideas for the Copilot's empty chat (Copilot Agent Builder §3.6).

A template is a starting sentence, not a configuration: its prompt starts Agent
mode (`/agent ...`) and the Copilot builds the real proposal from what the
tenant actually has. `requires_any` names plugin folders
(`McpConnection.config["_plugin_name"]`); an idea shows when one of them is
connected and visible to the member. Empty means generic. Editions add
templates through `EditionExtension.agent_templates()`; core keys win.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oc8 import models as m
from oc8.authz.scope import DepartmentScope

MAX_IDEAS: Final = 4


@dataclass(frozen=True)
class AgentTemplate:
    key: str
    title_en: str
    title_de: str
    subtitle_en: str
    subtitle_de: str
    requires_any: tuple[str, ...]
    prompt_en: str
    prompt_de: str


CORE_TEMPLATES: Final[tuple[AgentTemplate, ...]] = (
    AgentTemplate(
        key="invoice_checker",
        title_en="Invoice checker",
        title_de="Rechnungsprüfer",
        subtitle_en="Checks open invoices every morning",
        subtitle_de="Prüft jeden Morgen offene Rechnungen",
        requires_any=("odoo_mcp",),
        prompt_en=(
            "/agent Set up an invoice checker that reviews open invoices every morning "
            "and tells me what stands out."
        ),
        prompt_de=(
            "/agent Leg mir einen Rechnungsprüfer an, der jeden Morgen offene Rechnungen "
            "prüft und mir Auffälligkeiten meldet."
        ),
    ),
    AgentTemplate(
        key="support_triage",
        title_en="Support triage",
        title_de="Support-Triage",
        subtitle_en="Sorts new tickets and drafts first answers",
        subtitle_de="Sortiert neue Tickets und entwirft erste Antworten",
        requires_any=("odoo_mcp", "jira_mcp"),
        prompt_en=(
            "/agent Set up a support triage agent that sorts new tickets by urgency and "
            "drafts first answers."
        ),
        prompt_de=(
            "/agent Leg mir einen Agenten an, der neue Tickets nach Dringlichkeit "
            "sortiert und erste Antworten entwirft."
        ),
    ),
    AgentTemplate(
        key="weekly_sales_report",
        title_en="Weekly sales report",
        title_de="Wöchentlicher Vertriebsbericht",
        subtitle_en="Sums up the pipeline every Monday",
        subtitle_de="Fasst jeden Montag die Pipeline zusammen",
        requires_any=("odoo_mcp", "hubspot_mcp"),
        prompt_en="/agent Set up an agent that sends me a short sales report every Monday morning.",
        prompt_de=(
            "/agent Leg mir einen Agenten an, der mir jeden Montagmorgen einen kurzen "
            "Vertriebsbericht schickt."
        ),
    ),
    AgentTemplate(
        key="lead_qualification",
        title_en="Lead qualification",
        title_de="Lead-Qualifizierung",
        subtitle_en="Scores new leads and suggests the next step",
        subtitle_de="Bewertet neue Leads und schlägt den nächsten Schritt vor",
        requires_any=("hubspot_mcp",),
        prompt_en=(
            "/agent Set up an agent that qualifies new leads and suggests the next step for each."
        ),
        prompt_de=(
            "/agent Leg mir einen Agenten an, der neue Leads bewertet und für jeden den "
            "nächsten Schritt vorschlägt."
        ),
    ),
    AgentTemplate(
        key="meeting_prep",
        title_en="Meeting prep",
        title_de="Terminvorbereitung",
        subtitle_en="Briefs you before every customer meeting",
        subtitle_de="Brieft dich vor jedem Kundentermin",
        requires_any=("microsoft365", "google_workspace"),
        prompt_en=(
            "/agent Set up an agent that prepares a short brief before each of my "
            "customer meetings."
        ),
        prompt_de=(
            "/agent Leg mir einen Agenten an, der vor jedem Kundentermin ein kurzes "
            "Briefing vorbereitet."
        ),
    ),
    AgentTemplate(
        key="contract_deadlines",
        title_en="Contract deadline watcher",
        title_de="Vertragsfristen im Blick",
        subtitle_en="Warns you before notice periods end",
        subtitle_de="Warnt dich, bevor Kündigungsfristen ablaufen",
        requires_any=("microsoft365", "google_workspace", "gdrive_source"),
        prompt_en=(
            "/agent Set up an agent that watches our contracts and warns me before a "
            "notice period ends."
        ),
        prompt_de=(
            "/agent Leg mir einen Agenten an, der unsere Verträge im Blick behält und "
            "mich vor Ablauf einer Kündigungsfrist warnt."
        ),
    ),
    AgentTemplate(
        key="knowledge_faq",
        title_en="Knowledge base FAQ",
        title_de="Wissensdatenbank-FAQ",
        subtitle_en="Answers colleagues' questions from your documents",
        subtitle_de="Beantwortet Fragen aus euren Dokumenten",
        requires_any=(),
        prompt_en=(
            "/agent Set up an agent that answers my colleagues' questions from our knowledge base."
        ),
        prompt_de=(
            "/agent Leg mir einen Agenten an, der Fragen meiner Kollegen aus unserer "
            "Wissensdatenbank beantwortet."
        ),
    ),
    AgentTemplate(
        key="onboarding_buddy",
        title_en="Onboarding buddy",
        title_de="Onboarding-Begleiter",
        subtitle_en="Helps new colleagues through their first weeks",
        subtitle_de="Begleitet neue Kollegen durch die ersten Wochen",
        requires_any=(),
        prompt_en=(
            "/agent Set up an onboarding buddy that helps new colleagues through their first weeks."
        ),
        prompt_de=(
            "/agent Leg mir einen Onboarding-Begleiter an, der neue Kollegen durch die "
            "ersten Wochen begleitet."
        ),
    ),
)


def merge_templates(
    core: Sequence[AgentTemplate], extra: Iterable[AgentTemplate]
) -> list[AgentTemplate]:
    """Core first; an edition template whose key core already uses is ignored."""
    merged = list(core)
    seen = {t.key for t in merged}
    for template in extra:
        if template.key not in seen:
            merged.append(template)
            seen.add(template.key)
    return merged


def pick_ideas(
    templates: Sequence[AgentTemplate], connected: frozenset[str], limit: int = MAX_IDEAS
) -> list[AgentTemplate]:
    matching = [t for t in templates if t.requires_any and connected & set(t.requires_any)]
    generic = [t for t in templates if not t.requires_any]
    return (matching + generic)[:limit]


async def connected_kinds(
    db: AsyncSession, *, tenant_id: uuid.UUID, scope: DepartmentScope
) -> frozenset[str]:
    """Plugin names of connected systems this member can see: tenant-wide ones,
    and department ones only in a department they may view."""
    rows = (
        (
            await db.execute(
                select(m.McpConnection).where(
                    m.McpConnection.tenant_id == tenant_id, m.McpConnection.connected.is_(True)
                )
            )
        )
        .scalars()
        .all()
    )
    kinds: set[str] = set()
    for row in rows:
        if row.department_id is not None and not scope.may_view(row.department_id):
            continue
        name = (row.config or {}).get("_plugin_name")
        if isinstance(name, str) and name:
            kinds.add(name)
    return frozenset(kinds)
