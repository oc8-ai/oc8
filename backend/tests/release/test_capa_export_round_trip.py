"""Proves the ZIP an export produces is genuinely disk-compatible: unzipped
into a real capas/ root, `discovery.py` must read it back unmodified and
`install_plugin`/`instantiate_department` must reconstruct the same
Department/Agent/Trigger shape in a FRESH tenant (Capa-Exporter design,
Testing Strategy)."""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from oc8 import models as m
from oc8.capas.discovery import discover_plugins
from oc8.capas.export import _render, build_department_export, build_skill_export
from oc8.capas.export_package import build_zip
from oc8.capas.manifest import (
    DepartmentTemplateSpec,
    Manifest,
    TemplateAgent,
    TemplateAgentTrigger,
)
from oc8.capas.materialise import materialise_plugin_data
from oc8.capas.service import install_plugin, instantiate_department
from tests.conftest import AppSessionFactory

_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


@pytest.mark.asyncio
async def test_export_unzip_install_round_trip(
    app_session: AppSessionFactory, tmp_path: Path
) -> None:
    source_tenant = uuid.uuid4()
    async with app_session(source_tenant) as s:
        skill = m.Skill(tenant_id=source_tenant, name="crm-follow-up", origin="local")
        s.add(skill)
        await s.flush()
        skill_version = m.SkillVersion(
            tenant_id=source_tenant,
            skill_id=skill.id,
            semver="1.0.0",
            definition={
                "schema_version": 1,
                "instruction": "follow up",
                "requires": {"tools": [], "kbs": []},
            },
            artifact_hash=b"x",
        )
        s.add(skill_version)
        await s.flush()
        skill.current_version_id = skill_version.id

        # frame.kbs is a real, populated field a live tenant would have --
        # not present here would make the later "kbs never crosses over"
        # assertion tautological (Task 4 review, Important #1).
        source_kb_id = str(uuid.uuid4())
        dept = m.Department(
            tenant_id=source_tenant,
            name="Vertrieb",
            frame={"tools": {}, "memory": {}, "kbs": [source_kb_id]},
        )
        s.add(dept)
        await s.flush()
        # model_config_id likewise must be populated on the source row, or
        # asserting it's None on the target proves nothing was ever stripped.
        lead = m.Agent(
            tenant_id=source_tenant,
            department_id=dept.id,
            name="Nora",
            mission="Lead sales",
            is_team_lead=True,
            status="stopped",
            model_config_id=uuid.uuid4(),
        )
        s.add(lead)
        await s.flush()
        dept.team_lead_agent_id = lead.id
        rep = m.Agent(
            tenant_id=source_tenant,
            department_id=dept.id,
            name="Rep A",
            mission="Follow up leads",
            status="stopped",
        )
        s.add(rep)
        await s.flush()
        s.add(
            m.SkillAssignment(
                tenant_id=source_tenant, agent_id=rep.id, skill_version_id=skill_version.id
            )
        )
        s.add(
            m.Trigger(
                tenant_id=source_tenant,
                agent_id=lead.id,
                kind="cron",
                task_text="Tagesreport erstellen",
                cron_expression="0 8 * * 1-5",
                enabled=True,
            )
        )
        # A non-cron trigger on the OTHER agent: only kind="cron" is
        # exportable (design's Non-Goals), so this must leave no trace in
        # the manifest or the target tenant (Task 4 review, Important #2).
        webhook_token = uuid.uuid4().hex
        s.add(
            m.Trigger(
                tenant_id=source_tenant,
                agent_id=rep.id,
                kind="webhook",
                task_text="Handle inbound webhook",
                webhook_token=webhook_token,
                enabled=True,
            )
        )
        await s.flush()

        skill_export_item = await build_skill_export(
            s,
            tenant_id=source_tenant,
            skill_id=skill.id,
            capa_name="crm_follow_up",
            version="1.0.0",
            summary="",
        )
        dept_export = await build_department_export(
            s,
            tenant_id=source_tenant,
            department_id=dept.id,
            capa_name="vertrieb",
            version="1.0.0",
            summary="Vertriebsteam",
        )

    # The webhook trigger's secret token, and the word "webhook" itself,
    # must never reach the rendered manifest -- direct proof at the render
    # boundary, before we even get to proving no Trigger row survives.
    assert webhook_token not in dept_export.manifest_toml
    assert "webhook" not in dept_export.manifest_toml
    assert source_kb_id not in dept_export.manifest_toml
    assert str(lead.model_config_id) not in dept_export.manifest_toml
    # Cross-checked against the same blanket UUID-shape scan the dedicated
    # boundary test runs, on the actual output of this real export call.
    assert not _UUID_PATTERN.search(dept_export.manifest_toml)
    assert not _UUID_PATTERN.search(skill_export_item.manifest_toml)

    zip_bytes = build_zip([skill_export_item, dept_export])
    capas_root = tmp_path / "capas"
    capas_root.mkdir()
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        zf.extractall(capas_root)

    discovered = discover_plugins(paths=[str(capas_root)])
    by_name = {p.plugin_id: p for p in discovered}
    assert set(by_name) == {"crm_follow_up", "vertrieb"}
    assert all(p.valid for p in by_name.values()), [
        p.error for p in by_name.values() if not p.valid
    ]
    skill_manifest = by_name["crm_follow_up"].manifest
    dept_manifest = by_name["vertrieb"].manifest
    assert skill_manifest is not None
    assert dept_manifest is not None

    target_tenant = uuid.uuid4()
    async with app_session(target_tenant) as s:
        skill_version_row = await install_plugin(
            s, tenant_id=target_tenant, manifest_data=skill_manifest
        )
        await materialise_plugin_data(s, tenant_id=target_tenant, version=skill_version_row)

        dept_version = await install_plugin(s, tenant_id=target_tenant, manifest_data=dept_manifest)
        new_dept = await instantiate_department(
            s, tenant_id=target_tenant, version=dept_version, name="Vertrieb"
        )

        new_agents = (
            (await s.execute(select(m.Agent).where(m.Agent.department_id == new_dept.id)))
            .scalars()
            .all()
        )
        assert {a.name for a in new_agents} == {"Nora", "Rep A"}
        new_lead = next(a for a in new_agents if a.name == "Nora")
        new_rep = next(a for a in new_agents if a.name == "Rep A")
        assert new_dept.team_lead_agent_id == new_lead.id
        assert new_lead.mission == "Lead sales"
        assert new_rep.mission == "Follow up leads"

        # Trigger round-tripped.
        new_trigger = (
            await s.execute(select(m.Trigger).where(m.Trigger.agent_id == new_lead.id))
        ).scalar_one()
        assert new_trigger.cron_expression == "0 8 * * 1-5"
        assert new_trigger.task_text == "Tagesreport erstellen"
        # A hand-built Trigger row (bypassing triggers/service.py::create_trigger)
        # never got next_run_at set -- the scheduler only ever selects
        # `next_run_at <= now` (triggers/scheduler.py), so a NULL there can
        # never be selected and the trigger would be permanently dead despite
        # existing in the database. Asserting it's set is the only way this
        # test would have caught that -- it only checked the two content
        # fields before.
        assert new_trigger.next_run_at is not None

        # Skill assignment round-tripped (Task 1's fix).
        new_skill = (
            await s.execute(
                select(m.Skill).where(
                    m.Skill.tenant_id == target_tenant, m.Skill.name == "crm-follow-up"
                )
            )
        ).scalar_one()
        assignment = (
            await s.execute(
                select(m.SkillAssignment).where(m.SkillAssignment.agent_id == new_rep.id)
            )
        ).scalar_one()
        assert assignment.skill_version_id == new_skill.current_version_id

        # The webhook trigger did not survive: Rep A gets no Trigger row at
        # all in the target tenant (only kind="cron" is exportable).
        rep_triggers = (
            (await s.execute(select(m.Trigger).where(m.Trigger.agent_id == new_rep.id)))
            .scalars()
            .all()
        )
        assert rep_triggers == []

        # Non-Goals: a genuine proof, not a tautology -- the source rows
        # above actually populated model_config_id and frame.kbs, so their
        # absence here proves the export path strips them rather than
        # merely never having anything to strip.
        assert new_dept.id != dept.id
        assert lead.model_config_id is not None
        assert new_lead.model_config_id is None
        assert "kbs" not in (new_dept.frame or {})


@pytest.mark.asyncio
async def test_tool_pack_export_round_trips_a_custom_manual_http_capa(
    app_session: AppSessionFactory,
) -> None:
    from oc8.capas.export import build_tool_pack_export
    from oc8.capas.service import install_plugin

    tenant = uuid.uuid4()
    manifest = {
        "name": "acme_billing",
        "version": "1.0.0",
        "type": "tool_pack",
        "summary": "Acme's billing REST API.",
        "tool_pack": {
            "connections": [
                {
                    "key": "default",
                    "name": "acme_billing",
                    "server_url": "https://api.acme.example/v1",
                    "transport": "manual_http",
                    "config": {
                        "auth_header_name": "Authorization",
                        "http_tools": [
                            {
                                "name": "get_invoice",
                                "description": "Fetch an invoice by id.",
                                "method": "GET",
                                "url_template": "/invoices/{id}",
                                "param_schema": {
                                    "type": "object",
                                    "properties": {"id": {"type": "string"}},
                                },
                            }
                        ],
                    },
                }
            ]
        },
        "setup": {
            "title": "Acme Billing",
            "fields": [
                {"key": "token", "label": "API token", "kind": "password"},
            ],
            "mcp": {
                "connection_key": "default",
                "name": "acme_billing",
                "secret_env_fields": {"Authorization": "token"},
            },
        },
    }
    async with app_session(tenant) as db:
        version = await install_plugin(
            db, tenant_id=tenant, manifest_data=manifest, origin="custom"
        )
        await db.commit()

    async with app_session(tenant) as db:
        exported = await build_tool_pack_export(db, tenant_id=tenant, capa_id=version.capa_id)
        assert exported.warnings == []
        assert "acme_billing" in exported.manifest_toml
        assert "get_invoice" in exported.manifest_toml

    # Re-install into a second tenant from the exported TOML alone.
    import tomllib

    # `_render()` wraps the manifest fields under a top-level [plugin] table
    # (the on-disk plugin.toml convention `discovery.py` reads) -- but
    # `install_plugin`/`parse_manifest` take the FLAT dict `Manifest` itself
    # validates against (the same shape `POST /capas`'s `InstallRequest.manifest`
    # is), so re-importing from the rendered TOML means unwrapping that table
    # first, exactly as a real "download export -> reinstall" flow would.
    reimport_manifest = tomllib.loads(exported.manifest_toml)["plugin"]
    other_tenant = uuid.uuid4()
    async with app_session(other_tenant) as db:
        reimported = await install_plugin(
            db, tenant_id=other_tenant, manifest_data=reimport_manifest, origin="custom"
        )
        await db.commit()
        assert reimported.semver == "1.0.0"


def test_no_uuid_anywhere_in_a_rendered_department_manifest() -> None:
    """The hard guarantee behind "no secrets/ids in the ZIP" -- not just a
    docstring claim. Walks the rendered TOML text itself (not a Python dict,
    since the actual artifact an operator receives IS the TOML string) and
    fails if any UUID-shaped substring appears anywhere. Calls export.py's
    OWN `_render()` (the exact function every `build_*_export` uses) rather
    than reimplementing the render step, so a future change to how a
    manifest is dumped to TOML can't silently drift out of sync with what
    this test actually checks."""
    # This is a static-content check: build a manifest with every optional
    # field populated with a real-shaped value and assert none of the export
    # code paths ever interpolate a raw id into a string field.
    manifest = Manifest(
        name="vertrieb",
        version="1.0.0",
        type="department_template",
        summary="Vertriebsteam",
        department_template=DepartmentTemplateSpec(
            frame={"tools": {"odoo": {"read": True}}, "memory": {}},
            agents=[
                TemplateAgent(
                    name="Nora",
                    mission="Lead sales",
                    is_team_lead=True,
                    trigger=TemplateAgentTrigger(
                        cron_expression="0 8 * * 1-5", task_text="Tagesreport erstellen"
                    ),
                ),
                TemplateAgent(
                    name="Rep A",
                    reports_to="Nora",
                    skills=["crm-follow-up"],
                    narrowing={"tools": {"odoo": {"read": True, "only": ["search_records"]}}},
                ),
            ],
        ),
        depends=["integration:odoo"],
    )

    rendered = _render(manifest)
    assert not _UUID_PATTERN.search(rendered), (
        "a rendered manifest must never contain a UUID-shaped string anywhere"
    )
