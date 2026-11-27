"""`agent_template.scope`: who may hire an agent template."""

from __future__ import annotations

import re
from typing import Any

import pytest
from pydantic import ValidationError

from oc8.capas.manifest import Manifest, parse_manifest


def _mf(**agent: Any) -> dict[str, Any]:
    return {
        "name": "mail_assistant",
        "version": "1.0.0",
        "type": "agent_template",
        "agent_template": {"name": "Mia", **agent},
    }


def test_scope_defaults_to_department() -> None:
    assert Manifest.model_validate(_mf()).agent_template.scope == "department"  # type: ignore[union-attr]


def test_scope_personal_is_accepted() -> None:
    mf = Manifest.model_validate(_mf(scope="personal"))
    assert mf.agent_template is not None
    assert mf.agent_template.scope == "personal"


def test_unknown_scope_is_refused() -> None:
    with pytest.raises(ValidationError):
        Manifest.model_validate(_mf(scope="team"))


def test_a_department_template_cannot_carry_a_personal_agent() -> None:
    with pytest.raises(
        ValueError,
        match=re.escape(
            "agent 'A' has scope 'personal'; a department template cannot carry a personal agent"
        ),
    ):
        parse_manifest(
            {
                "name": "sales",
                "version": "1.0.0",
                "type": "department_template",
                "department_template": {"agents": [{"name": "A", "scope": "personal"}]},
            }
        )
