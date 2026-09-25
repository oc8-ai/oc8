from __future__ import annotations

from pathlib import Path

import pytest
from oc8_evals.tasks import Task, load_suite, load_task, resolve_scenario

_TOML = """
id = "crm_qualify_lead"
systems = ["odoo"]
origin = "manual"
scenario = "crm_qualify_lead"
autonomy = "autonomous"
task_text = "Qualify the lead."
expects_question = false
expects_approval = false

[frame_tools.odoo]
enabled = true
read = true
modify = true

[budget]
max_steps = 30
max_tokens = 200000
"""


def test_a_task_file_loads(tmp_path: Path) -> None:
    p = tmp_path / "crm_qualify_lead.toml"
    p.write_text(_TOML)
    task = load_task(p)
    assert task == Task(
        id="crm_qualify_lead",
        systems=("odoo",),
        task_text="Qualify the lead.",
        origin="manual",
        scenario="crm_qualify_lead",
        autonomy="autonomous",
        frame_tools={"odoo": {"enabled": True, "read": True, "modify": True}},
        expects_question=False,
        expects_approval=False,
        max_steps=30,
        max_tokens=200000,
        code_mode=False,
        context_window_tokens=None,
    )


def test_optional_caps_load_from_toml(tmp_path: Path) -> None:
    p = tmp_path / "with_caps.toml"
    p.write_text(
        _TOML.replace("crm_qualify_lead", "with_caps").replace(
            "expects_approval = false\n",
            "expects_approval = false\ncode_mode = true\ncontext_window_tokens = 2000\n",
        )
    )
    task = load_task(p)
    assert task.code_mode is True
    assert task.context_window_tokens == 2000


def test_caps_default_off(tmp_path: Path) -> None:
    p = tmp_path / "crm_qualify_lead.toml"
    p.write_text(_TOML)
    task = load_task(p)
    assert task.code_mode is False
    assert task.context_window_tokens is None


def test_frame_tools_preserves_a_monetary_threshold_instead_of_coercing_to_bool(
    tmp_path: Path,
) -> None:
    # A department frame's approval_eur (odoo's value_spec seam) is an int
    # threshold, not a flag -- bool(3000) would silently become True and no
    # eval task could ever exercise the approval path (pre-flight ruling).
    p = tmp_path / "with_threshold.toml"
    toml_text = _TOML.replace("crm_qualify_lead", "with_threshold").replace(
        "modify = true", "modify = true\napproval_eur = 3000"
    )
    p.write_text(toml_text)
    task = load_task(p)
    assert task.frame_tools["odoo"]["approval_eur"] == 3000


def test_the_file_name_must_match_the_id(tmp_path: Path) -> None:
    p = tmp_path / "other.toml"
    p.write_text(_TOML)
    with pytest.raises(ValueError, match="file name"):
        load_task(p)


def test_a_suite_is_sorted_by_id(tmp_path: Path) -> None:
    for name in ("b_task", "a_task"):
        (tmp_path / f"{name}.toml").write_text(_TOML.replace("crm_qualify_lead", name))
    assert [t.id for t in load_suite(tmp_path)] == ["a_task", "b_task"]


def test_the_shipped_office_suite_has_ten_tasks_with_resolvable_scenarios() -> None:
    root = Path(__file__).resolve().parents[1] / "oc8_evals" / "suites" / "office"
    tasks = load_suite(root)
    assert len(tasks) == 10
    for t in tasks:
        scenario = resolve_scenario(t.scenario)
        for hook in ("setup", "expect", "forbid", "teardown"):
            assert callable(getattr(scenario, hook)), f"{t.id}: scenario lacks {hook}"
