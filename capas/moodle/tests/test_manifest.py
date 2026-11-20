from __future__ import annotations

import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from oc8.capas.discovery import find_plugin
from oc8.capas.manifest import Manifest, parse_manifest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = PLUGIN_ROOT.parent

READ = {
    "moodle_site_info",
    "moodle_search_courses",
    "moodle_get_course_contents",
    "moodle_list_participants",
    "moodle_find_users",
    "moodle_get_grades",
    "moodle_get_completion",
    "moodle_list_assignments",
    "moodle_list_submissions",
    "moodle_list_forum_discussions",
}
OUTWARD = [
    "moodle_grade_submission",
    "moodle_send_message",
    "moodle_post_to_forum",
]
IRREVERSIBLE = [*OUTWARD, "moodle_create_user", "moodle_enrol_user"]
NEVER_FOR_ADMIN = {"moodle_grade_submission", "moodle_send_message", "moodle_post_to_forum"}


def _evict() -> None:
    for _stale in [
        n
        for n in sys.modules
        if n in {"connector", "mcp_bridge"} or n.startswith(("connector.", "mcp_bridge."))
    ]:
        del sys.modules[_stale]


@pytest.fixture(autouse=True)
def _plugin_path() -> Iterator[None]:
    _evict()
    sys.path.insert(0, str(PLUGIN_ROOT))
    yield
    sys.path.remove(str(PLUGIN_ROOT))
    _evict()


def _manifest() -> Manifest:
    discovered = find_plugin("moodle", [str(PLUGINS_ROOT)])
    assert discovered is not None, "moodle was not discovered"
    assert discovered.valid, f"moodle manifest is invalid: {discovered.error}"
    assert discovered.manifest is not None
    return parse_manifest(discovered.manifest)


def _connection() -> Any:
    manifest = _manifest()
    assert manifest.tool_pack is not None
    assert len(manifest.tool_pack.connections) == 1
    return manifest.tool_pack.connections[0]


def test_the_core_manifest() -> None:
    manifest = _manifest()
    assert (manifest.name, manifest.type, manifest.trust) == ("moodle", "tool_pack", "first_party")
    assert manifest.version == "1.1.0"
    assert set(manifest.permissions) == {"mcp:connect", "secrets:use"}


def test_scopes_cover_every_served_tool_exactly_once() -> None:
    from mcp_bridge.__main__ import ALL_TOOLS

    scopes = _connection().scopes
    assert isinstance(scopes, dict)
    read, modify = set(scopes["read"]), set(scopes["modify"])
    assert not read & modify
    assert read == READ
    assert read | modify == {t.name for t in ALL_TOOLS}


def test_outward_and_destructive_tools_are_modify_tools_in_config() -> None:
    conn = _connection()
    assert conn.config["outward_tools"] == OUTWARD
    assert conn.config["irreversible_tools"] == IRREVERSIBLE
    # Account and enrolment mails are Moodle's own system mails: confirmed as
    # irreversible, but not outward, so one task may create or enrol many.
    for name in ("moodle_create_user", "moodle_enrol_user"):
        assert name not in conn.config["outward_tools"]
        assert name in conn.config["irreversible_tools"]
    assert conn.config["destructive_tools"] == ["moodle_unenrol_user"]
    modify = set(conn.scopes["modify"])
    assert set(IRREVERSIBLE) <= modify and "moodle_unenrol_user" in modify
    assert "outward_tools" not in conn.scopes


def test_the_bridge_launch_matches_the_package_on_disk() -> None:
    manifest = _manifest()
    conn = _connection()
    assert conn.transport == "stdio"
    assert conn.config["command"] == "python"
    assert conn.config["args"] == ["-m", "mcp_bridge"]
    assert conn.config["env"]["PYTHONPATH"] == "/app/capas/moodle"
    assert manifest.setup is not None and manifest.setup.mcp is not None
    assert manifest.setup.mcp.command == conn.config["command"]
    assert manifest.setup.mcp.args == conn.config["args"]
    assert (PLUGIN_ROOT / "mcp_bridge" / "__main__.py").is_file()


def test_the_credential_reaches_the_bridge_env() -> None:
    manifest = _manifest()
    conn = _connection()
    assert conn.credential_type == "moodle_token"
    assert conn.credential_env_fields == {"MOODLE_URL": "url", "MOODLE_TOKEN": "token"}
    assert manifest.setup is not None and manifest.setup.mcp is not None
    assert manifest.setup.mcp.env_fields == {"MOODLE_URL": "url"}
    assert manifest.setup.mcp.secret_env_fields == {"MOODLE_TOKEN": "site"}
    cred = tomllib.loads((PLUGIN_ROOT / "credential_types" / "moodle_token.toml").read_text())
    assert [(f["key"], f["kind"]) for f in cred["fields"]] == [
        ("url", "url"),
        ("token", "password"),
    ]


def test_the_setup_form() -> None:
    manifest = _manifest()
    assert manifest.setup is not None
    assert manifest.setup.title == "Set up Moodle"
    assert "docs.oc8.ai/user/integrations/moodle" in manifest.setup.description
    assert [f.key for f in manifest.setup.fields] == ["department", "site"]


def test_presets_and_library() -> None:
    conn = _connection()
    assert {p.key for p in conn.guardrail_presets} == {"read_only", "assist_with_approval"}
    assert [p.key for p in conn.guardrail_presets if p.recommended] == ["assist_with_approval"]
    discovered = find_plugin("moodle", [str(PLUGINS_ROOT)])
    assert discovered is not None and discovered.guardrail_library is not None
    assert {g.key for g in discovered.guardrail_library.guardrail} == {
        "teaching_assistant",
        "administration_with_approval",
        "communication_with_approval",
    }


def test_guardrails_name_real_tools_and_gate_modify() -> None:
    from mcp_bridge.__main__ import ALL_TOOLS

    actual = {t.name for t in ALL_TOOLS}
    for path in sorted((PLUGIN_ROOT / "guardrails").glob("*.toml")):
        entry = tomllib.loads(path.read_text())
        assert entry["key"] == path.stem, path
        assert set(entry.get("only", [])) <= actual, path
        if entry.get("modify"):
            assert entry["approval_actions"] == ["modify"], path


def test_every_modify_tool_writes_without_a_prior_read_through_core() -> None:
    """Search tools carry no identity, so core's read-before-write could never
    be satisfied for a course/user/forum write; approval shows the target."""
    from oc8.agent.harness.stages.b_read_before_write import read_before_write_denial
    from oc8.agent.harness.state import Ledger

    conn = _connection()
    assert conn.config["read_before_write"] is False
    for tool in conn.scopes["modify"]:
        denial = read_before_write_denial(
            tool=tool,
            tier="modify",
            identity=("user", "7"),
            ledger=Ledger(),
            connection="moodle",
            config=conn.config,
            label="user 7",
        )
        assert denial is None, tool


def test_read_only_preset_cannot_modify() -> None:
    conn = _connection()
    read_only = next(p for p in conn.guardrail_presets if p.key == "read_only")
    assert read_only.modify is False


def test_communication_preset_reads_and_reaches_learners_only() -> None:
    entry = tomllib.loads(
        (PLUGIN_ROOT / "guardrails" / "communication_with_approval.toml").read_text()
    )
    assert set(entry["only"]) == READ | {"moodle_send_message", "moodle_post_to_forum"}


def test_send_message_asks_for_one_call_per_reminder() -> None:
    from mcp_bridge.tools.communication import TOOLS

    send = next(t for t in TOOLS if t.name == "moodle_send_message")
    assert "ONE call" in (send.description or "")


def test_teaching_assistant_cannot_message_or_administer() -> None:
    entry = tomllib.loads((PLUGIN_ROOT / "guardrails" / "teaching_assistant.toml").read_text())
    assert set(entry["only"]) == READ | {"moodle_grade_submission"}


def test_administration_preset_never_grades_messages_or_posts() -> None:
    entry = tomllib.loads(
        (PLUGIN_ROOT / "guardrails" / "administration_with_approval.toml").read_text()
    )
    assert not set(entry["only"]) & NEVER_FOR_ADMIN
    assert {"moodle_enrol_user", "moodle_create_user", "moodle_create_course"} <= set(entry["only"])


def test_focus_spec_and_record_url_agree() -> None:
    conn = _connection()
    focus = conn.config["focus_spec"]
    assert conn.record_url is not None
    assert set(conn.record_url.models) <= set(focus["labels"])
    assert conn.record_url.base_url_path == ["env", "MOODLE_URL"]


def test_record_url_resolves_through_core_to_a_working_moodle_link() -> None:
    from oc8.approvals.record_url import resolve_record_url

    conn = _connection()
    cfg = {"env": {"MOODLE_URL": "https://lms.example.org"}}
    assert (
        resolve_record_url(conn.record_url, config=cfg, entity="course", ref="7")
        == "https://lms.example.org/course/view.php?id=7"
    )
    assert (
        resolve_record_url(conn.record_url, config=cfg, entity="student", ref="9")
        == "https://lms.example.org/user/view.php?id=9"
    )
