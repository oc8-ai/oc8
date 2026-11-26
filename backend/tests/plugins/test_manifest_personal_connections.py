"""A personal template's `connections_any`, `connections_optional` and `triggers`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from oc8.capas.discovery import discover_plugins
from oc8.capas.manifest import (
    Manifest,
    TemplateAgentEventTrigger,
    TemplateAgentTrigger,
)

CRON = {"cron_expression": "0 7 * * *", "task_text": "x"}
EVENT = {"kind": "event", "source": "a", "event_type": "item.created", "task_text": "y"}


def _agent(**agent: Any) -> Manifest:
    return Manifest.model_validate(
        {
            "name": "assistant",
            "version": "1.0.0",
            "type": "agent_template",
            "agent_template": {"name": "Mia", **agent},
        }
    )


def test_a_personal_template_declares_alternatives_and_triggers() -> None:
    mf = _agent(
        scope="personal",
        connections_any=["a", "b"],
        connections_optional=["c"],
        triggers=[CRON, EVENT],
    )
    tpl = mf.agent_template
    assert tpl is not None
    assert tpl.connections_any == ["a", "b"]
    assert tpl.connections_optional == ["c"]
    assert isinstance(tpl.triggers[0], TemplateAgentTrigger)
    assert isinstance(tpl.triggers[1], TemplateAgentEventTrigger)
    assert tpl.triggers[1].source == "a"
    assert tpl.triggers[1].config == {}


@pytest.mark.parametrize(
    "extra",
    [
        {"connections_any": ["a"]},
        {"connections_optional": ["a"]},
        {"triggers": [CRON]},
    ],
)
def test_a_department_template_cannot_declare_connections(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="personal"):
        _agent(**extra)


def test_an_event_trigger_must_name_a_declared_connection() -> None:
    with pytest.raises(ValidationError, match="'z'"):
        _agent(
            scope="personal",
            connections_any=["a"],
            triggers=[{**EVENT, "source": "z"}],
        )


def test_an_event_trigger_may_name_an_optional_connection() -> None:
    _agent(scope="personal", connections_optional=["a"], triggers=[EVENT])


def test_alternatives_and_optional_do_not_overlap() -> None:
    with pytest.raises(ValidationError, match="both"):
        _agent(scope="personal", connections_any=["a"], connections_optional=["a"])


def test_a_connection_is_not_listed_twice() -> None:
    with pytest.raises(ValidationError, match="twice"):
        _agent(scope="personal", connections_any=["a", "a"])


def test_a_connection_name_is_not_empty() -> None:
    with pytest.raises(ValidationError, match="empty"):
        _agent(scope="personal", connections_optional=[" "])


def test_at_most_ten_triggers() -> None:
    with pytest.raises(ValidationError, match="10"):
        _agent(scope="personal", triggers=[CRON] * 11)


def test_a_cron_entry_parses_without_kind() -> None:
    mf = _agent(scope="personal", triggers=[CRON])
    assert mf.agent_template is not None
    entry = mf.agent_template.triggers[0]
    assert isinstance(entry, TemplateAgentTrigger)
    assert entry.kind == "cron"


def test_existing_single_trigger_still_parses() -> None:
    mf = _agent(trigger=CRON)
    assert mf.agent_template is not None
    assert mf.agent_template.trigger == TemplateAgentTrigger(**CRON)
    assert mf.agent_template.triggers == []


def test_trigger_and_triggers_are_exclusive() -> None:
    with pytest.raises(ValidationError, match="not both"):
        _agent(scope="personal", trigger=CRON, triggers=[CRON])


def test_a_non_personal_template_gets_no_event_trigger() -> None:
    with pytest.raises(ValidationError):
        _agent(trigger=EVENT)


def test_every_shipped_capa_still_validates() -> None:
    root = Path(__file__).resolve().parents[3] / "capas"
    found = discover_plugins([str(root)])
    assert found
    assert [(p.plugin_id, p.error) for p in found if p.error] == []
