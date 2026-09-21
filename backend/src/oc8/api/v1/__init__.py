"""/api/v1 router aggregation."""

from __future__ import annotations

from fastapi import APIRouter

from oc8.api.v1 import (
    agents,
    agents_write,
    approvals,
    audit,
    auth,
    backup,
    budgets,
    capas,
    catalog,
    channels,
    chat,
    clarifications,
    components,
    contracts,
    copilot,
    credentials,
    dashboard,
    departments,
    events,
    feed,
    files,
    flows,
    governance,
    handoffs,
    i18n,
    knowledge,
    kpis,
    mcp,
    mcp_logins,
    members,
    model_prices,
    notifications,
    oauth,
    onboarding,
    reconciliation,
    reports,
    roles,
    run,
    runtimes,
    secrets,
    settings,
    skills_write,
    supervision,
    tasks,
    totp,
    triggers,
    webhooks,
)

api_router = APIRouter()
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(totp.router, tags=["auth"])
api_router.include_router(i18n.router, tags=["i18n"])
api_router.include_router(departments.router, tags=["departments"])
api_router.include_router(onboarding.router, tags=["onboarding"])
api_router.include_router(agents.router, tags=["agents"])
api_router.include_router(agents_write.router, tags=["agents"])
api_router.include_router(catalog.router, tags=["catalog"])
api_router.include_router(model_prices.router, tags=["catalog"])
api_router.include_router(reconciliation.router, tags=["catalog"])
api_router.include_router(channels.router, tags=["channels"])
api_router.include_router(chat.router, tags=["chat"])
api_router.include_router(files.router, tags=["files"])
api_router.include_router(skills_write.router, tags=["skills"])
api_router.include_router(budgets.router, tags=["budgets"])
api_router.include_router(knowledge.router, tags=["knowledge"])
api_router.include_router(feed.router, tags=["feed"])
api_router.include_router(approvals.router, tags=["approvals"])
api_router.include_router(clarifications.router, tags=["approvals"])
api_router.include_router(tasks.router, tags=["tasks"])
api_router.include_router(components.router, tags=["components"])
api_router.include_router(members.router, tags=["members"])
api_router.include_router(mcp.router, tags=["mcp"])
api_router.include_router(mcp_logins.router, tags=["mcp"])
api_router.include_router(notifications.router, tags=["notifications"])
api_router.include_router(dashboard.router, tags=["dashboard"])
api_router.include_router(oauth.router, tags=["oauth"])
api_router.include_router(capas.router, tags=["capas"])
api_router.include_router(roles.router, tags=["roles"])
api_router.include_router(run.router, tags=["run"])
api_router.include_router(reports.router, tags=["reports"])
api_router.include_router(runtimes.router, tags=["runtimes"])
# internal_agent is deliberately NOT included here: the operator API refuses
# agent tokens as a whole (see main.py), and this is the one path under
# /api/v1 that an agent token legitimately uses. It is mounted on the app
# directly so that exception is visible in one place instead of implied.
api_router.include_router(settings.router, tags=["settings"])
api_router.include_router(events.router, tags=["events"])
api_router.include_router(handoffs.router, tags=["handoffs"])
api_router.include_router(supervision.router, tags=["supervision"])
api_router.include_router(contracts.router, tags=["contracts"])
api_router.include_router(copilot.router, tags=["copilot"])
api_router.include_router(flows.router, tags=["flows"])
api_router.include_router(governance.router, tags=["governance"])
api_router.include_router(triggers.router, tags=["triggers"])
api_router.include_router(webhooks.router, tags=["webhooks"])
api_router.include_router(secrets.router, tags=["secrets"])
api_router.include_router(credentials.router, tags=["credentials"])
api_router.include_router(audit.router, tags=["audit"])
api_router.include_router(backup.router, tags=["backup"])
api_router.include_router(kpis.router, tags=["kpis"])
