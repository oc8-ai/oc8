"""Spec §3.2 invariant 1: the harness names no vendor, product, model or
software-specific field. Everything specific arrives through tool_pack.toml
or MCP annotations. Comments count -- a stray "e.g. Odoo" in a docstring is
how the next reader learns to special-case it."""

from __future__ import annotations

import re
from pathlib import Path

import oc8.agent.harness as harness_pkg

_DENYLIST = (
    "odoo",
    "salesforce",
    "hubspot",
    "jira",
    "github",
    "microsoft",
    "outlook",
    "gmail",
    "google",
    "sap",
    "crm.lead",
    "sale.order",
    "res.partner",
    "helpdesk.ticket",
    "openai",
    "anthropic",
    "claude",
    "gpt",
    "gemini",
    "deepseek",
    "opaas",
)
_WORD = re.compile("|".join(re.escape(w) for w in _DENYLIST), re.IGNORECASE)


def test_harness_sources_name_no_vendor_product_or_model() -> None:
    root = Path(harness_pkg.__file__).parent
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            if _WORD.search(line):
                offenders.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
